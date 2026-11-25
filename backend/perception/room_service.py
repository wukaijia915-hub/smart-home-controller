# -*- coding: utf-8 -*-
"""房间服务：节点接入 → 四态融合 → 六路旁证 → 事件 → 前端/感知上下文。

这是 CSI 接主链的编排层：

    MQTT/回放 ──► NodeRegistry ──► fuse_rooms() ──► corroborate()
                                                        │
                             ┌──────────────────────────┴────────────┐
                             ▼                                       ▼
                     RoomState（四态+证据）                 状态变化 → 事件
                             │                                       │
                   /api/rooms /api/rooms/{id}/timeline      EventRouter（no_person 等）

两条硬约束：
- 只有 `empty_confident` 才会发 `no_person` 事件；`uncertain` 永不触发关灯/关空调；
- `degraded` 只下调置信度并告警，绝不推断无人。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from ..config_loader import load_room_fusion, load_wifi_csi
from ..ingest import EspectreMQTTClient, NodeRegistry
from .corroboration import corroborate
from .rooms import (DEGRADED, EMPTY_CONFIDENT, PRESENT_CONFIDENT, UNCERTAIN,
                    RoomState, RoomTimeline, fuse_rooms)


class RoomService:
    def __init__(self, registry, store=None,
                 sensors_getter: Optional[Callable[[], dict]] = None,
                 wifi_config: Optional[dict] = None,
                 room_config: Optional[dict] = None,
                 now_fn: Optional[Callable[[], float]] = None):
        self.registry = registry
        self.store = store
        self.sensors_getter = sensors_getter or (lambda: {})
        self.wifi_config = wifi_config if wifi_config is not None else load_wifi_csi()
        self.room_config = room_config if room_config is not None else load_room_fusion()
        self._now = now_fn or time.time
        self.nodes = NodeRegistry.from_config(self.wifi_config, now_fn=self._now)
        self.timeline = RoomTimeline()
        self.states: dict[str, RoomState] = {}
        self.client: Optional[EspectreMQTTClient] = None
        self.last_events: list[dict] = []
        self._last_emit: dict[tuple[str, str], float] = {}
        self._pir_last: dict[str, float] = {}
        self._door_last: Optional[float] = None
        self._last_tick: float = 0.0

    # ---------- 接入 ----------
    def start_ingest(self) -> dict:
        """启动 MQTT 订阅。未启用 / 未装 paho 时返回原因，不抛异常。"""
        if self.client is None:
            self.client = EspectreMQTTClient(self.wifi_config, self.nodes)
        started = self.client.start()
        return {"started": started, **self.client.status()}

    def stop_ingest(self) -> None:
        if self.client is not None:
            self.client.stop()

    def reload_config(self) -> dict:
        """配置热重载：重新读节点清单与融合/旁证阈值（节点表按新配置重建）。"""
        self.wifi_config = load_wifi_csi()
        self.room_config = load_room_fusion()
        self.nodes = NodeRegistry.from_config(self.wifi_config, now_fn=self._now)
        if self.client is not None:
            self.client.config = self.wifi_config
            self.client.registry = self.nodes
        return {"nodes": len(self.nodes.nodes),
                "rooms": len(self.room_config.get("rooms") or [])}

    # ---------- 主循环 ----------
    def tick(self, now: Optional[float] = None) -> dict:
        """跑一轮：sweep → 融合 → 旁证 → 状态变化事件。返回本轮结果。"""
        now = float(now if now is not None else self._now())
        self._last_tick = now
        node_states = self.nodes.states(now)
        fused = fuse_rooms(node_states, self.room_config, now)

        previous = dict(self.states)
        fresh_states: dict[str, RoomState] = {}
        events: list[dict] = []
        for room_id, room in fused.items():
            evidence = self._collect_evidence(room_id, now)
            result = corroborate(room, evidence, self.room_config, now)
            final = result.room
            fresh_states[room_id] = final
            if self.timeline.append(final):
                pass
            event = self._transition_event(previous.get(room_id), final, now)
            if event:
                events.append(event)

        self.states = fresh_states
        self.last_events = events
        return {"now": now, "rooms": self.summary(), "events": events,
                "timeline_size": len(self.timeline)}

    def _transition_event(self, old: Optional[RoomState], new: RoomState,
                          now: float) -> Optional[dict]:
        if old is None or old.state == new.state:
            return None
        cfg = self.room_config.get("events") or {}
        if not cfg.get("emit_on_transition", True):
            return None
        mapping = {
            PRESENT_CONFIDENT: (cfg.get("present_event", "person_detected"), "info"),
            EMPTY_CONFIDENT: (cfg.get("empty_event", "no_person"), "notice"),
            DEGRADED: (cfg.get("degraded_event", "room_degraded"), "warning"),
        }
        mapped = mapping.get(new.state)
        if not mapped:
            return None
        event_type, severity = mapped
        cooldown = float(cfg.get("cooldown_seconds", 120) or 120)
        key = (new.room_id, event_type)
        if now - self._last_emit.get(key, 0.0) < cooldown:
            return None
        self._last_emit[key] = now
        return {
            "type": event_type,
            "source": "room_service",
            "severity": severity,
            "payload": {
                "room_id": new.room_id,
                "room": new.label,
                "from": old.state,
                "to": new.state,
                "confidence": new.confidence,
                "summary": _summary_text(new, old.state),
                "reason": (new.evidence or {}).get("reason", ""),
                "sources": [s.get("source") for s in
                            ((new.evidence or {}).get("corroboration") or {}).get("sources", [])],
            },
        }

    # ---------- 旁证采集 ----------
    def _collect_evidence(self, room_id: str, now: float) -> dict:
        sensors = self.sensors_getter() or {}
        # PIR：优先 pir_<room>，回退到既有 motion_<room> 仿真量
        pir_value = sensors.get(f"pir_{room_id}")
        if pir_value is None:
            pir_value = sensors.get(f"motion_{room_id}")
        pir = None
        if pir_value is not None:
            if bool(pir_value):
                self._pir_last[room_id] = now
            pir = {"motion": bool(pir_value),
                   "last_motion_at": self._pir_last.get(room_id)}

        # 插座功率（仿真量）：plug_power_<room>
        power = sensors.get(f"plug_power_{room_id}")
        if power is None:
            power = 0.0
        # 门磁（仿真量）
        door_open = sensors.get("door_entry_open")
        door = None
        if door_open is not None:
            if bool(door_open):
                self._door_last = now
            door = {"open": bool(door_open), "last_open_at": self._door_last}

        return {
            "pir": pir,
            "power_draw": float(power or 0.0),
            "recent_actions": self._recent_actions(room_id, now),
            "door": door,
            "camera": None,
        }

    def _recent_actions(self, room_id: str, now: float) -> list[dict]:
        if self.store is None:
            return []
        label = self._room_label(room_id)
        window = float(((self.room_config.get("corroboration") or {})
                        .get("action_window_seconds", 300)) or 300)
        out = []
        for entry in self.store.audit_log[-200:]:
            if float(entry.get("ts", 0)) < now - window:
                continue
            device = self.registry.get(entry.get("device_id", ""))
            if device is not None and getattr(device, "room", "") == label:
                out.append({"device_id": entry.get("device_id", ""),
                            "command": entry.get("command", ""),
                            "ts": entry.get("ts", 0)})
        return out

    def _room_label(self, room_id: str) -> str:
        for room in (self.room_config.get("rooms") or []):
            if room.get("room_id") == room_id:
                return str(room.get("label") or room_id)
        return room_id

    # ---------- 视图 ----------
    def summary(self) -> dict:
        """给感知上下文/前端：只有派生状态，没有原始 CSI。"""
        out = {}
        for room_id, room in self.states.items():
            corr = ((room.evidence or {}).get("corroboration") or {})
            out[room_id] = {
                "room_id": room_id,
                "label": room.label,
                "state": room.state,
                "confidence": round(float(room.confidence), 3),
                "reason": (room.evidence or {}).get("reason", ""),
                "empty_candidate": bool((room.evidence or {}).get("empty_candidate")),
                "conflict": corr.get("conflict", ""),
                "sources": [s.get("source") for s in corr.get("sources", [])],
                "updated_at": round(float(room.updated_at), 3),
            }
        return out

    def status(self) -> dict:
        return {
            "enabled": bool(self.wifi_config.get("enabled", False)),
            "nodes": self.nodes.status(),
            "rooms": [room.to_dict() for room in self.states.values()],
            "timeline_size": len(self.timeline),
            "ingest": self.client.status() if self.client is not None else {
                "started": False,
                "reason": "尚未调用 start_ingest()",
            },
            "last_tick": round(self._last_tick, 3),
        }

    def timeline_recent(self, room_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        return self.timeline.recent(room_id=room_id, limit=limit)

    # ---------- 回放（无硬件演示 / 测试） ----------
    def replay(self, records: list[dict]) -> dict:
        """按记录里的 received_at 逐条喂节点并 tick（不依赖真实时钟）。

        记录形状：{"node_id","state","received_at","score"?}
        这是答辩现场无硬件时的演示入口，也是 tests_csi 的集成测试入口。
        """
        results = []
        all_events = []
        for rec in records or []:
            node_id = rec.get("node_id", "")
            state = rec.get("state", "idle")
            at = float(rec.get("received_at", self._last_tick or self._now()))
            self.nodes.update_motion(node_id, state, rec.get("score"),
                                     rec.get("device_ts_ms"), received_at=at)
            tick = self.tick(now=at)
            all_events.extend(tick.get("events", []))
            results.append(tick)
        return {"ticks": len(results), "last": results[-1] if results else None,
                "states": self.summary(), "events": all_events}


def _summary_text(room: RoomState, previous: str) -> str:
    if room.state == EMPTY_CONFIDENT:
        return f"{room.label}判为无人（置信度 {room.confidence:.2f}），旁证一致"
    if room.state == PRESENT_CONFIDENT:
        return f"{room.label}检测到有人"
    if room.state == DEGRADED:
        return f"{room.label}感知降级，状态不再推断为无人"
    return f"{room.label}状态不确定（原 {previous}）"
