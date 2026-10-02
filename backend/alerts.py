# -*- coding: utf-8 -*-
"""内环告警评估：传感器读数 → 结构化事件。

原来这段逻辑是 `main.py::alert_loop` 里成排的 `if s.get("temp_indoor", 25) > 30:`
—— 阈值散在代码里、改一次要重启、且和设备故障检测（另一类完全不同的判断）
混在一个函数里。现在拆成：

    读数 + 声明式阈值(config/alert_routes.json)  →  结构化事件  →  EventRouter
                                                                  (决定动作与播报)

**两个配置各管一件事**，不要合并：`alert_routes.json` 管「什么时候算异常」，
`event_routes.json` 管「异常了做什么」。这两件事的变化频率完全不同 ——
调阈值天天有，改联动方式很少。

**与感知层的界限（答辩必问，务必分清）**：本模块是**确定性阈值判定**：
`sensors["temp_indoor"] > 30`，读数直接比大小，给定输入必然给定输出，阈值写在
配置里可被任何人复核。感知层是多模态**统计推断**（置信度融合、情境分类），
它的失效模式不透明。因此：**本层允许触发设备动作（走 EventRouter），
感知层绝对不允许**（见 CLAUDE.md 红线 1）。二者外观相似，性质不同。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .config_loader import load_alert_routes

_TPL_FIELD = re.compile(r"\{(\w+)(?::[^}]*)?\}")

_OPS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}


class AlertEvaluator:
    def __init__(self, config: dict | None = None):
        self.config = config if config is not None else load_alert_routes()
        self._last_fired: dict[str, float] = {}
        self._fired_count = 0

    def reload(self) -> None:
        self.config = load_alert_routes()

    # ---------- 主入口 ----------
    def evaluate(self, sensors: dict, devices: list = (), now: float | None = None) -> list[dict]:
        """返回本轮应当发出的告警事件（已过冷却过滤）。不产生任何设备动作。

        事件里带 `payload.summary`（渲染好的中文句子）。EventRouter 的播报动作
        模板用 `{summary}` 取它，因此**文案只在配置里写一处** —— 原来
        「渲染告警文案」和「播报文案」分散在两处的写法，改一处忘另一处
        会出现"播报的阈值和配置里的阈值不一致"。
        """
        now = time.monotonic() if now is None else now
        events = []
        for src in self.config.get("sources", []) or []:
            event = self._eval_sensor_source(src, sensors, now)
            if event:
                events.append(event)
        events.extend(self._eval_device_health(devices, now))
        self._fired_count += len(events)
        return events

    # ---------- 传感器阈值 ----------
    def _eval_sensor_source(self, src: dict, sensors: dict, now: float) -> dict | None:
        sensor = src.get("sensor", "")
        if sensor not in sensors:
            return None
        value = sensors.get(sensor)
        if not isinstance(value, (int, float)):
            return None
        op = _OPS.get(str(src.get("op", ">")))
        threshold = src.get("value")
        if op is None or not isinstance(threshold, (int, float)):
            return None
        if not op(value, threshold):
            return None
        # 附加条件：如"有人活动时才提示补光"，避免对着空房子报"光线偏暗"
        required = src.get("require_any_sensor") or []
        if required and not any(sensors.get(k) for k in required):
            return None
        if self._in_cooldown(f"src:{src.get('id')}", src, now):
            return None
        message = self._render(src.get("message_template", ""), value=value, threshold=threshold)
        return {
            "type": src.get("event_type", "sensor_alert"),
            "source": "inner_loop",
            "severity": src.get("severity", "info"),
            "payload": {
                "kind": src.get("kind", src.get("id", "")),
                "alert_id": src.get("id", ""),
                "sensor": sensor,
                "value": value,
                "threshold": threshold,
                "summary": message,
            },
        }

    # ---------- 设备健康 ----------
    def _eval_device_health(self, devices, now: float) -> list[dict]:
        cfg = self.config.get("device_health") or {}
        if not cfg:
            return []
        out = []
        for d in devices or []:
            state = getattr(d, "state", "online")
            device_id = getattr(d, "device_id", "")
            if state not in ("offline", "fault"):
                # 恢复后清掉冷却记录，否则设备第二次离线时会被上一次的冷却吃掉
                self._last_fired.pop(f"device:{device_id}:offline", None)
                self._last_fired.pop(f"device:{device_id}:fault", None)
                continue
            key = f"device:{device_id}:{state}"
            if self._in_cooldown(key, cfg, now):
                continue
            label = "离线" if state == "offline" else "故障"
            message = self._render(cfg.get("message_template", ""),
                                   device_name=getattr(d, "name", device_id),
                                   state_label=label)
            out.append({
                "type": cfg.get("event_type", "device_health"),
                "source": "inner_loop",
                "severity": cfg.get("severity", "warning"),
                "payload": {"kind": state, "device_id": device_id,
                            "device_name": getattr(d, "name", device_id),
                            "state": state, "summary": message},
            })
        return out

    # ---------- 冷却 ----------
    def _in_cooldown(self, key: str, cfg: dict, now: float) -> bool:
        cooldown = float(cfg.get("cooldown_seconds", 0) or 0)
        if cooldown <= 0:
            return False
        last = self._last_fired.get(key)
        if last is not None and now - last < cooldown:
            return True
        self._last_fired[key] = now
        return False

    # ---------- 文案 ----------
    @staticmethod
    def _render(template: str, **kwargs) -> str:
        """渲染模板，容错缺口。

        模板来自 JSON 配置（用户可改），写错占位符是常态。`str.format` 遇到
        未知字段会抛 KeyError，进而让整个告警循环崩掉 —— 为了一个文案错字
        把告警功能整个搞停是不划算的。这里只替换认识的字段，其余留空。
        """
        if not template:
            return ""
        def sub(m):
            return f"{kwargs[m.group(1)]:.0f}" if isinstance(kwargs.get(m.group(1)), float) \
                else str(kwargs.get(m.group(1), ""))
        return _TPL_FIELD.sub(sub, template)

    def stats(self) -> dict:
        return {
            "sources": len(self.config.get("sources", []) or []),
            "device_health_rule": bool(self.config.get("device_health")),
            "fired_total": self._fired_count,
            "active_cooldowns": len(self._last_fired),
        }

    def reset(self) -> None:
        self._last_fired.clear()
        self._fired_count = 0


alerts = AlertEvaluator()
