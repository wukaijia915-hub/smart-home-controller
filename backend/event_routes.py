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

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
    """把结构化事件路由为「设备控制 + 告警通知」的声明式动作。"""

    def __init__(self, registry, manager=None, routes_path: Optional[Path] = None,
                 state_store=None):
        self.registry = registry            # DeviceRegistry（含回读确认）
        self.manager = manager              # ConnectionManager（数字人广播，可空）
        self.state_store = state_store      # StateStore（审计，可空）
        self.routes_path = routes_path or CONFIG_PATH
        self.raw: dict[str, Any] = {}
        self._last_action_at: dict[str, float] = {}
        self.load()

    def load(self) -> None:
        if not self.routes_path.exists():
            self.raw = {"routes": []}
            return
        try:
            self.raw = json_load(self.routes_path)
        except Exception:
            self.raw = {"routes": []}

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
            if len(action_specs) <= 1:
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
        result = self.registry.control(device_id, command, params)
        result = dict(result)
        result.update({"route_id": route_id, "type": "device"})
        if self.state_store is not None and result.get("ok"):
            self.state_store.record(None, device_id, command, params, result)
        return result

    def _invoke_alert(self, route_id: str, event: dict[str, Any],
                      spec: dict[str, Any]) -> dict[str, Any]:
        template = str(spec.get("message_template", "{summary}"))
        message = template.format(
            event_type=event.get("type", ""),
            source=event.get("source", ""),
            summary=str((event.get("payload") or {}).get("summary", "") or event.get("type", "")),
        )
        result = {
            "route_id": route_id,
            "type": "alert",
            "message": message,
            "status": "delivered" if self.manager is not None else "queued",
        }
        if self.manager is not None:
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self.manager.broadcast({
                        "type": "agent_reply",
                        "reply": message,
                        "digital_human": {"emotion": "alert", "action": "speak"},
                        "actions": [],
                        "decision_trace": [],
                        "requires_confirmation": False,
                        "pending_id": "",
                        "pending_action": None,
                    }))
                finally:
                    loop.close()
            except Exception:
                result["status"] = "queued"
        return result

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
