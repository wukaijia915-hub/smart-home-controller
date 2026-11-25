# -*- coding: utf-8 -*-
"""白盒情境参数：可解释、可复核的家庭情境判断。

## 与 fusion.py 的分工

| | 输入 | 输出 | 特征 |
|---|---|---|---|
| `fusion.py` | 各模态的 V/A 判定 | 融合后的氛围倾向 | 置信度加权 + 一致性复核；由多模态证据交叉验证 |
| `situation.py` | 传感器 + 操作日志 + 时间 | 5 个情境参数 + 情境类别 | 每个参数都有**可指认的真值来源**，可人工复核 |

两者互补：融合回答"各方面证据是否一致地指向某个氛围"，
情境参数回答"家里现在到底处于什么状态、依据是哪几个传感器哪个时间段"。

**本模块刻意不产出情绪或心理判断。** 它产出的是行为/环境事实的归纳
（在家多久、活动多少、作息是否偏离、环境是否不适、有没有异常行为），
这些都有明确真值，不依赖任何未经验证的测量假设。

## 可解释性

每个 `SituationParam` 都带 `evidence`（哪几个传感器、哪个时间窗、算出来多少）
与 `truth`（真值来源）。前端「情境看板」展开任一参数即可看到这些原始依据 ——
这是"可解释性"从口号变成可查证据的地方。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "situation_mapping.json"

# 无历史数据时作息偏离度取中性值（不是 0，也不是"最差"）
NEUTRAL_ROUTINE_DEVIATION = 1.0


class PresenceTracker:
    """在传感器循环里累积"有人"的连续时长。

    「在家时长」需要的是**时序**信息，而单次快照只有当前在不在。
    这个累加器由 main.py 的 sensor_loop 定期喂入，是"在家时长"这一参数的真值来源。
    重启服务会归零 —— 这一点在 `to_dict()` 里如实暴露，不假装是长期统计。
    """

    def __init__(self):
        self.minutes = 0.0
        self.present = False
        self._last_ts: Optional[datetime] = None
        self._last_activity: Optional[datetime] = None

    def update(self, present: bool, now: Optional[datetime] = None) -> float:
        """喂入一次在位状态，返回累计在位分钟数。"""
        now = now or datetime.now()
        if self._last_ts is not None:
            delta_min = max(0.0, (now - self._last_ts).total_seconds() / 60.0)
            if present:
                self.minutes += delta_min
            if present:
                self._last_activity = now
        else:
            # 首次调用无历史间隔，按 0 计，避免凭空多出一段时长
            self._last_activity = now if present else None
        self.present = present
        self._last_ts = now
        return self.minutes

    def idle_minutes(self, now: Optional[datetime] = None) -> float:
        """距上次检测到活动已过多少分钟。"""
        if self._last_activity is None:
            return 0.0
        now = now or datetime.now()
        return max(0.0, (now - self._last_activity).total_seconds() / 60.0)

    def reset(self) -> None:
        self.minutes = 0.0
        self._last_ts = None
        self._last_activity = None
        self.present = False


@dataclass
class SituationInput:
    """情境评估的输入。"""
    sensors: dict = field(default_factory=dict)
    recent_actions: list = field(default_factory=list)
    presence_minutes: float = 0.0
    idle_minutes: float = 0.0
    history: dict = field(default_factory=dict)
    now: Optional[datetime] = None
    #: 光照/人体推断的"疑久卧"时长（无摄像头时为 0，表示无此证据）
    lying_minutes: float = 0.0


@dataclass
class SituationParam:
    """一个白盒情境参数。"""
    name: str
    label: str
    value: float
    normalized: float          # [0,1]
    weight: float
    unit: str = ""
    source: str = ""
    truth: str = ""
    evidence: dict = field(default_factory=dict)
    confidence: float = 1.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "value": round(self.value, 3),
            "normalized": round(self.normalized, 3),
            "weight": self.weight,
            "unit": self.unit,
            "source": self.source,
            "truth": self.truth,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 3),
            "note": self.note,
        }


@dataclass
class SituationState:
    """一次情境评估的完整结果。"""
    params: dict = field(default_factory=dict)
    score: float = 0.0              # 加权综合，越大越"偏离平稳"
    region: str = ""
    region_label: str = ""
    region_level: str = "info"
    reason: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "params": {k: v.to_dict() for k, v in self.params.items()},
            "score": round(self.score, 3),
            "region": self.region,
            "region_label": self.region_label,
            "region_level": self.region_level,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
        }


class SituationAssessor:
    """按声明式配置计算情境参数并归类。"""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or CONFIG_PATH
        self.config: dict = {}
        self.load()

    def load(self) -> None:
        try:
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            self.config = {}

    # ---------- 各参数 ----------
    @staticmethod
    def _normalize(value: float, span: list) -> float:
        lo, hi = float(span[0]), float(span[1])
        if hi <= lo:
            return 0.0
        return max(0.0, min(1.0, (value - lo) / (hi - lo)))

    def _comfort_offset(self, sensors: dict) -> tuple[float, dict]:
        band = self.config.get("comfort_band", {})
        offsets, detail = [], {}
        for key, (lo, hi) in band.items():
            if key not in sensors:
                continue
            value = float(sensors[key])
            width = max(float(hi) - float(lo), 1e-6)
            if value < lo:
                off = min(1.0, (lo - value) / width)
            elif value > hi:
                off = min(1.0, (value - hi) / width)
            else:
                off = 0.0
            offsets.append(off)
            detail[key] = {"value": value, "band": [lo, hi], "offset": round(off, 3)}
        if not offsets:
            return 0.0, {}
        return sum(offsets) / len(offsets), detail

    def _anomaly_flags(self, inp: SituationInput) -> tuple[int, dict]:
        """统计命中的异常行为标志，并**记录每条规则的检查结果**。

        即使一条都没命中，也要留下"查过哪些规则、阈值多少"的依据 ——
        "检查了 3 条规则、都没命中"本身就是可复核的证据，空字典不是。
        """
        rules = self.config.get("anomaly_rules", {})
        now = inp.now or datetime.now()
        flags, detail = 0, {}

        # 深夜活动：活跃时段内操作数超阈值
        r = rules.get("late_night_activity") or {}
        in_window = now.hour in (r.get("active_hours") or [])
        hit_night = in_window and len(inp.recent_actions) >= r.get("min_actions", 3)
        detail["late_night_activity"] = {
            "label": r.get("label", "深夜活动"),
            "hit": hit_night,
            "hour": now.hour,
            "actions": len(inp.recent_actions),
            "threshold": r.get("min_actions", 3),
        }
        flags += 1 if hit_night else 0

        # 长时间无活动：有人在位但久无动作。
        # 判定必须用 idle_minutes（距上次活动多久），不能用"近期操作条数" ——
        # 操作少不等于长时间没动，后者才有时间维度的含义
        r = rules.get("prolonged_inactive") or {}
        idle_limit = r.get("idle_minutes", 180)
        hit_idle = inp.presence_minutes > 0 and inp.idle_minutes >= idle_limit
        detail["prolonged_inactive"] = {
            "label": r.get("label", "长时间无活动"),
            "hit": hit_idle,
            "idle_minutes": round(inp.idle_minutes, 1),
            "threshold": idle_limit,
        }
        flags += 1 if hit_idle else 0

        # 久卧：需要额外的卧床证据（无摄像头时为 0，不参与判定）
        r = rules.get("prolonged_lying") or {}
        lying_limit = r.get("lying_minutes", 120)
        hit_lying = inp.lying_minutes >= lying_limit
        detail["prolonged_lying"] = {
            "label": r.get("label", "久卧"),
            "hit": hit_lying,
            "lying_minutes": round(inp.lying_minutes, 1),
            "threshold": lying_limit,
            "available": inp.lying_minutes > 0,
        }
        flags += 1 if hit_lying else 0

        return flags, detail

    # ---------- 主入口 ----------
    def assess(self, inp: SituationInput) -> SituationState:
        spec = self.config.get("params", {})

        comfort_value, comfort_detail = self._comfort_offset(inp.sensors or {})
        anomaly_count, anomaly_detail = self._anomaly_flags(inp)

        raw = {
            "home_duration": inp.presence_minutes,
            "activity": float(len(inp.recent_actions)),
            "routine_deviation": float(inp.history.get("routine_deviation",
                                                        NEUTRAL_ROUTINE_DEVIATION)),
            "comfort_offset": comfort_value,
            "anomaly_count": float(anomaly_count),
        }
        evidence = {
            "home_duration": {"presence_minutes": round(inp.presence_minutes, 1),
                              "present_now": bool(inp.sensors.get("motion_living")
                                                  or inp.sensors.get("motion_bedroom")),
                              "note": "服务重启后归零，非长期统计"},
            "activity": {"recent_actions": len(inp.recent_actions),
                         "window": "近期操作窗口"},
            "routine_deviation": ({"routine_deviation": raw["routine_deviation"]}
                                  if "routine_deviation" in (inp.history or {})
                                  else {"note": "无历史基线，取中性值",
                                        "neutral_value": NEUTRAL_ROUTINE_DEVIATION}),
            "comfort_offset": comfort_detail,
            "anomaly_count": anomaly_detail,
        }

        params, weighted_sum, weight_total = {}, 0.0, 0.0
        for name, value in raw.items():
            cfg = spec.get(name, {})
            norm = self._normalize(value, cfg.get("normalize", [0, 1]))
            weight = float(cfg.get("weight", 1.0))
            # 置信度：有真值来源的满分；靠中性兜底的降权（作息偏离度在无数据时）
            conf = 1.0
            note = ""
            if name == "routine_deviation" and "routine_deviation" not in (inp.history or {}):
                conf = 0.3
                note = "无历史基线，取中性值并降低置信度"
            params[name] = SituationParam(
                name=name, label=cfg.get("label", name), value=value, normalized=norm,
                weight=weight, unit=cfg.get("unit", ""), source=cfg.get("source", ""),
                truth=cfg.get("truth", ""), evidence=evidence.get(name, {}),
                confidence=conf, note=note,
            )
            weighted_sum += norm * weight * conf
            weight_total += weight * conf

        score = weighted_sum / weight_total if weight_total else 0.0
        region, level, reason = self._classify(params, inp)

        # 整体置信度 = 各参数置信度的加权平均
        confidence = (sum(p.confidence * p.weight for p in params.values())
                      / sum(p.weight for p in params.values())) if params else 0.0

        return SituationState(params=params, score=score, region=region,
                              region_label=reason, region_level=level,
                              reason=reason, confidence=confidence)

    def _classify(self, params: dict, inp: SituationInput) -> tuple[str, str, str]:
        """按配置中的规则列表归类，首个命中者胜（兜底规则 when 为空，须在最后）。

        判定可用的量分两类：
        - 5 个评分参数（nums）—— 它们有条数归一化区间，是综合评分的组成部分
        - 时序原始量（idle_minutes / lying_minutes）—— 不参与评分，
          但作为判定依据更直接（"多久没动"比"最近操作了几次"更贴近语义）

        **规则顺序即优先级**，配置文件里从最具体到最宽泛排列，
        兜底规则 `normal` 的 when 为空（恒真）必须放最后。
        """
        nums = {k: p.value for k, p in params.items()}
        nums.setdefault("idle_minutes", float(inp.idle_minutes))
        nums.setdefault("lying_minutes", float(inp.lying_minutes))
        nums.setdefault("presence_minutes", float(inp.presence_minutes))

        ops = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
               "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
               "==": lambda a, b: a == b}
        for region in self.config.get("regions", []):
            conditions = region.get("when") or []
            if all(ops.get(c.get("op"), lambda a, b: False)(nums.get(c["param"], 0.0),
                                                            c["value"])
                   for c in conditions):
                label = region.get("label", region.get("id", ""))
                detail = "，".join(f"{c['param']}{c['op']}{c['value']}"
                                   for c in conditions) or "兜底规则"
                return region.get("id", ""), region.get("level", "info"), \
                    f"{label}（判定条件：{detail}）"
        return "unknown", "info", "无可匹配的情境规则"
