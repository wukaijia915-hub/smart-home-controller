# -*- coding: utf-8 -*-
"""声明式事件路由引擎（主动式证据链闭环的触发器）。

物理世界事件（跌倒检测 / 人员离开 / 安全隐患）→ 匹配声明式路由配置
→ 并发执行多设备动作（fan-out）+ 数字人告警 → 每条动作结果带执行状态
（confirmed/delivered/failed）与回读证据，写入 StateStore 审计。

声明式路由 + 冷却时间 + 三段式执行状态的设计思想参考 UAgentOS
EventRouter（Apache-2.0），按灵犀架构重写（JSON 配置 + registry.control
回读确认 + ConnectionManager 广播），结构与其 YAML 实现不同。
"""
from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config_loader import data_path

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "event_routes.json"

SEVERITY_RANK = {
    "debug": 0, "info": 1, "notice": 2, "warning": 3,
    "warn": 3, "alert": 4, "critical": 5, "emergency": 6,
}


@dataclass
class RouteEvaluation:
    """一次事件路由的完整评估结果（供审计与前端可视化）。"""
    event: dict[str, Any]
    matched_routes: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    ignored_routes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "matched_routes": self.matched_routes,
            "actions": self.actions,
            "ignored_routes": self.ignored_routes,
        }


class EventRouter:
    """把结构化事件路由为「设备控制 + 告警通知」的声明式动作。

    路由来源有两个，**分开存放、合并生效**：
    - `backend/config/event_routes.json`：随仓库发布的预置路由（只读语义）；
    - `data/event_routes_custom.json`：用户经「草稿→确认」创建的规则（可增删）。
    合并后由同一个 `route()` 执行，冷却/高危拒绝/回读证据对两者一视同仁 ——
    用户创建的规则不是"另一条执行路径"，只是另一份配置。
    """

    def __init__(self, registry, manager=None, routes_path: Optional[Path] = None,
                 state_store=None, loop=None, custom_path: Optional[Path] = None):
        self.registry = registry            # DeviceRegistry（含回读确认）
        self.manager = manager              # ConnectionManager（数字人广播，可空）
        self.state_store = state_store      # StateStore（审计 + 版本递增，可空）
        self.routes_path = routes_path or CONFIG_PATH
        # 自定义路由落在运行时状态目录（LINGXI_DATA_DIR 可覆盖），测试天然隔离
        self.custom_path = custom_path or data_path("event_routes_custom.json")
        self.raw: dict[str, Any] = {}
        self._base_routes: list[dict] = []
        self._custom_routes: list[dict] = []
        self._last_action_at: dict[str, float] = {}
        self._loop = loop                   # 主事件循环（由 lifespan 注入，见 set_loop）
        self.load()

    def set_loop(self, loop) -> None:
        """注入主事件循环引用。

        必须在 lifespan 中调用：EventRouter 在模块导入期构造，那时还没有运行中的
        事件循环。缺省时 _invoke_alert 只能返回 "queued"，数字人播报不会真的发出。
        """
        self._loop = loop

    def load(self) -> None:
        self._base_routes = self._read_routes(self.routes_path)
        self._custom_routes = self._read_routes(self.custom_path)
        self.raw = {"routes": [*self._base_routes, *self._custom_routes]}

    @staticmethod
    def _read_routes(path) -> list[dict]:
        if not path:
            return []
        p = Path(path)
        if not p.exists():
            return []
        try:
            data = json_load(p)
        except Exception:
            return []
        routes = data.get("routes") if isinstance(data, dict) else None
        return [r for r in (routes or []) if isinstance(r, dict)]

    # ---------- 自定义路由的增删（草稿确认后调用） ----------
    def add_route(self, route: dict) -> dict:
        """追加一条自定义路由并落盘。id 冲突时自动加后缀，绝不覆盖预置路由。"""
        self.load()
        route = dict(route)
        rid = str(route.get("id") or "")
        if not rid:
            rid = f"user_{uuid.uuid4().hex[:8]}"
        existing = {str(r.get("id") or "") for r in self._custom_routes}
        if rid in existing or any(str(r.get("id") or "") == rid for r in self._base_routes):
            rid = f"{rid}_{uuid.uuid4().hex[:4]}"
        route["id"] = rid
        self._custom_routes = [*self._custom_routes, route]
        self._save_custom()
        self.load()
        return route

    def remove_route(self, route_id: str) -> bool:
        """删除一条**自定义**路由。预置路由不可删（返回 False）。"""
        self.load()
        kept = [r for r in self._custom_routes if str(r.get("id") or "") != route_id]
        if len(kept) == len(self._custom_routes):
            return False
        self._custom_routes = kept
        self._save_custom()
        self.load()
        return True

    def list_custom(self) -> list[dict]:
        self.load()
        return list(self._custom_routes)

    def list_base(self) -> list[dict]:
        """预置路由（只读，随仓库发布）。"""
        self.load()
        return list(self._base_routes)

    def _save_custom(self) -> None:
        path = Path(self.custom_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json_dumps({"version": 1, "routes": self._custom_routes}),
            encoding="utf-8",
        )
        os.replace(tmp, path)      # 原子替换：半截文件不会被下一次 load 读到

    # ---------- 主入口 ----------
    def route(self, event: dict[str, Any]) -> RouteEvaluation:
        self.load()
        evaluation = RouteEvaluation(event=event)
        for route in self.raw.get("routes", []) or []:
            route_id = str(route.get("id", "unnamed_route"))
            if not self._matches(event, route.get("match", {}) or {}):
                evaluation.ignored_routes.append(route_id)
                continue
            evaluation.matched_routes.append(route_id)

            cooldown = float(route.get("cooldown_seconds", 0) or 0)
            if cooldown > 0:
                elapsed = time.monotonic() - self._last_action_at.get(route_id, 0.0)
                if elapsed < cooldown:
                    evaluation.actions.append({
                        "route_id": route_id,
                        "type": "cooldown",
                        "status": "skipped",
                        "detail": {"cooldown_seconds": cooldown, "elapsed_seconds": round(elapsed, 3)},
                    })
                    continue
                self._last_action_at[route_id] = time.monotonic()

            action_specs = route.get("actions", []) or []
            # 同一台设备上的多条动作必须串行。并行 fan-out 的执行顺序不确定，
            # 「先 turn_on 再 set_temperature」如果颠倒，中间会存在一个
            # "开着但目标温度还是旧值"的瞬间被回读抓到，回读证据链就会显示不一致。
            # 跨设备才用并行（慢通知不该阻塞播报）。
            same_device = len({str(s.get("device_id", "")) for s in action_specs}) < len(action_specs)
            if len(action_specs) <= 1 or route.get("sequential") or same_device:
                for spec in action_specs:
                    evaluation.actions.append(self._invoke_action(route_id, event, spec))
            else:
                # 独立动作并行 fan-out：慢通知不能阻塞现场播报/设备联动
                with ThreadPoolExecutor(max_workers=min(len(action_specs), 8),
                                       thread_name_prefix="lx-route") as pool:
                    futures = [pool.submit(self._invoke_action, route_id, event, spec)
                               for spec in action_specs]
                    evaluation.actions.extend(f.result() for f in futures)
        return evaluation

    # ---------- 匹配 ----------
    def _matches(self, event: dict[str, Any], match: dict[str, Any]) -> bool:
        event_types = match.get("event_types") or []
        if event_types and event.get("type") not in event_types:
            return False
        sources = match.get("sources") or []
        if sources and event.get("source") not in sources:
            return False
        min_severity = match.get("severity_at_least")
        if min_severity:
            severity = event.get("severity") or event.get("payload", {}).get("severity") or "info"
            if SEVERITY_RANK.get(str(severity).lower(), 1) < SEVERITY_RANK.get(str(min_severity).lower(), 0):
                return False
        payload = event.get("payload") or {}
        for key in match.get("payload_has") or []:
            if _lookup(payload, str(key), missing=None) is None:
                return False
        for key, expected in (match.get("payload_equals") or {}).items():
            if _lookup(payload, str(key), missing=None) != expected:
                return False
        return True

    # ---------- 动作执行 ----------
    def _invoke_action(self, route_id: str, event: dict[str, Any],
                       spec: dict[str, Any]) -> dict[str, Any]:
        action_type = str(spec.get("type", "device"))
        if action_type == "alert":
            return self._invoke_alert(route_id, event, spec)
        return self._invoke_device(route_id, event, spec)

    def _invoke_device(self, route_id: str, event: dict[str, Any],
                       spec: dict[str, Any]) -> dict[str, Any]:
        device_id = self._resolve_value(spec.get("device_id", ""), event)
        command = self._resolve_value(spec.get("command", ""), event)
        params = {k: self._resolve_value(v, event) for k, v in (spec.get("params", {}) or {}).items()}

        # 高危动作默认拒绝（fail closed）。
        # 事件路由是**无人确认**的自动路径：没有弹窗、没有人点"确认执行"。
        # 此前它直接调 registry.control()，意味着只要有人往 event_routes.json 里
        # 加一条指向 lock_entry_01 / gas_stove_kitchen_01 的联动，门锁就会在无人
        # 值守时被打开、燃气灶会被点燃 —— 与"高危动作必须二次确认"的宣称直接矛盾。
        # 需要自动执行高危动作的路由，必须在配置里显式写 "allow_high_risk": true，
        # 让这个决定被写下来、被评审看到，而不是悄悄发生。
        refused = self._refuse_high_risk(device_id, command, spec)
        if refused is not None:
            refused.update({"route_id": route_id, "type": "device"})
            return refused

        # 唯一写入口：事件路径也走 execution，从而拿到与对话路径同款的
        # 审计（before/after/readback）、六段链与撤销记录。
        # 高危拒绝已在上面 _refuse_high_risk 完成，这里 allow_high_risk=True
        # 只是"放行已经显式授权的动作"，不是绕过检查。
        from . import execution
        result = execution.execute_action(
            device_id, command, params,
            registry=self.registry, store=self.state_store, source="event_route",
            allow_high_risk=True,
            causal={
                "signal": {"event_type": event.get("type", ""),
                           "source": event.get("source", ""),
                           "payload": event.get("payload") or {}},
                "inference": {"route_id": route_id, "spec": dict(spec)},
                "gate": {"refuse_high_risk": "passed_or_explicitly_allowed"},
            })
        result.update({"route_id": route_id, "type": "device"})
        return result

    def _refuse_high_risk(self, device_id: str, command: str, spec: dict) -> Optional[dict]:
        """高危命令且未显式授权 → 返回拒绝结果；否则返回 None。"""
        if spec.get("allow_high_risk"):
            return None
        try:
            from .lease import LeaseValidator
        except Exception:
            return None
        device = self.registry.get(device_id) if hasattr(self.registry, "get") else None
        if device is None:
            return None        # 设备不存在由 registry.control 自己报错，不在这里越权判断
        if not LeaseValidator.is_high_risk(device.category, command):
            return None
        desc = LeaseValidator.high_risk_desc(device.category, command)
        return {
            "ok": False,
            "status": "refused",
            "device_id": device_id,
            "command": command,
            "error": f"事件路由拒绝自动执行高危动作「{desc}」（{device.name}）："
                     f"该动作需要人工二次确认，自动路径不得执行。"
                     f"确需自动执行时，请在 event_routes.json 该动作上显式声明 "
                     f'"allow_high_risk": true。',
        }

    def _invoke_alert(self, route_id: str, event: dict[str, Any],
                      spec: dict[str, Any]) -> dict[str, Any]:
        template = str(spec.get("message_template", "{summary}"))
        message = template.format(
            event_type=event.get("type", ""),
            source=event.get("source", ""),
            summary=str((event.get("payload") or {}).get("summary", "") or event.get("type", "")),
        )
        status = self._broadcast_alert(message)
        return {
            "route_id": route_id,
            "type": "alert",
            "message": message,
            "status": status,
        }

    def _broadcast_alert(self, message: str) -> str:
        """把数字人播报投递回主事件循环，返回投递状态（delivered / queued）。

        按调用上下文分四种情况处理：
        - 已注入 loop + 处于工作线程（/api/events/route 经 asyncio.to_thread）
          → run_coroutine_threadsafe
        - 已注入 loop + 就在该循环线程上（内环告警循环）
          → create_task，避免 run_coroutine_threadsafe 自锁
        - 未注入 loop + 当前无运行中循环（纯同步上下文：测试 / 脚本）
          → asyncio.run 临时循环跑到完成
        - 未注入 loop + 却有运行中循环
          → 无法安全跨循环投递，返回 queued（不嵌套建循环）

        旧实现无条件 asyncio.new_event_loop() + run_until_complete，
        在协程内会触发 "Cannot run the event loop while another loop is running"。
        """
        if self.manager is None:
            return "queued"
        payload = {
            "type": "agent_reply",
            "reply": message,
            "digital_human": {"emotion": "alert", "action": "speak"},
            "actions": [],
            "decision_trace": [],
            "requires_confirmation": False,
            "pending_id": "",
            "pending_action": None,
        }
        try:
            import asyncio
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            if self._loop is None:
                if running is not None:
                    return "queued"
                asyncio.run(self.manager.broadcast(payload))
                return "delivered"
            if running is self._loop:
                running.create_task(self.manager.broadcast(payload))
            else:
                asyncio.run_coroutine_threadsafe(self.manager.broadcast(payload), self._loop)
            return "delivered"
        except Exception:
            return "queued"

    # ---------- 模板解析 ----------
    @staticmethod
    def _resolve_value(value: Any, event: dict[str, Any]) -> Any:
        if isinstance(value, str) and value.startswith("$event."):
            return _lookup_event(event, value[len("$event."):])
        if isinstance(value, dict):
            return {k: EventRouter._resolve_value(v, event) for k, v in value.items()}
        if isinstance(value, list):
            return [EventRouter._resolve_value(item, event) for item in value]
        return value


def _lookup_event(event: dict[str, Any], path: str) -> Any:
    if path == "type":
        return event.get("type", "")
    if path == "source":
        return event.get("source", "")
    if path.startswith("payload."):
        return _lookup(event.get("payload") or {}, path[len("payload."):], missing="")
    return ""


def _lookup(payload: dict[str, Any], path: str, missing: Any = "") -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return missing
    return current


def json_load(path: Path) -> dict[str, Any]:
    import json
    return json.loads(path.read_text(encoding="utf-8")) or {"routes": []}


def json_dumps(data: Any) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, indent=2)
