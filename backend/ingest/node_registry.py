# -*- coding: utf-8 -*-
"""ESPectre 节点注册表：节点（Node）不是可控设备（Device）。

## 为什么要把两个时间戳分开

- `last_seen`：任意消息（health / wifi / motion / sensing）的到达时间，只回答"节点还活着吗"；
- `last_event_at`：只记录 `motion` / `idle` 事件的到达时间，回答"运动数据还新鲜吗"。

`stale` 由 `last_event_at` 判定，**不能**由 health 心跳续命。设备连着、事件流断了，
是最危险的一类"看起来像没人"——MQTT 连接存活不等于 CSI 数据可用（见 ESPectre API 文档）。

## 时间基准

跨节点一律用**后端接收时间**（`received_at`），设备 `timestamp_ms` 只作为设备内排序的
调试字段保存。设备时钟不可跨节点比较，也不与后端时钟直接相减。

节点与设备分离：Node 不进 `/api/devices`，也不产生租约或设备动作。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

VALID_STATES = ("motion", "idle", "unknown")


@dataclass
class Node:
    """一个 ESPectre 节点的运行时状态。"""

    node_id: str
    room_id: str
    label: str = ""
    state: str = "unknown"                 # motion | idle | unknown
    score: Optional[float] = None          # 跨节点不可比，只在本节点内有意义
    rssi_dbm: Optional[int] = None
    calibration_state: str = "unknown"
    last_seen: float = 0.0                 # 任意消息的后端接收时间
    last_event_at: float = 0.0             # motion/idle 的后端接收时间
    last_motion_at: Optional[float] = None
    idle_since: Optional[float] = None     # 连续 idle 的起点（不被重复 idle 重置）
    device_ts_ms: Optional[int] = None     # 设备单调时钟，仅调试/排序用
    stale: bool = True
    online: bool = False

    def idle_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """连续 idle 的秒数；有运动或从未 idle 时返回 None / 0。"""
        if self.state == "motion":
            return 0.0
        if self.idle_since is None:
            return None
        return max(0.0, float(now if now is not None else time.time()) - self.idle_since)

    def to_dict(self, now: Optional[float] = None) -> dict:
        idle = self.idle_seconds(now)
        return {
            "node_id": self.node_id,
            "room_id": self.room_id,
            "label": self.label,
            "state": self.state,
            "score": self.score,
            "rssi_dbm": self.rssi_dbm,
            "calibration_state": self.calibration_state,
            "online": self.online,
            "stale": self.stale,
            "last_seen": round(self.last_seen, 3) if self.last_seen else 0.0,
            "last_event_at": round(self.last_event_at, 3) if self.last_event_at else 0.0,
            "last_motion_at": round(self.last_motion_at, 3) if self.last_motion_at else None,
            "idle_seconds": round(idle, 1) if idle is not None else None,
            "device_ts_ms": self.device_ts_ms,
        }


class NodeRegistry:
    """线程安全的节点表。MQTT 回调线程写，主循环/HTTP 读。"""

    def __init__(self, nodes: Optional[list] = None,
                 stale_after_seconds: float = 30.0,
                 online_after_seconds: float = 60.0,
                 now_fn: Optional[Callable[[], float]] = None):
        self._lock = threading.RLock()
        self._now = now_fn or time.time
        self.stale_after_seconds = float(stale_after_seconds)
        self.online_after_seconds = float(online_after_seconds)
        self.nodes: dict[str, Node] = {}
        self._stats = {
            "motion": 0,
            "liveness": 0,
            "wifi": 0,
            "sensing": 0,
            "rejected": 0,
            "unknown_node": 0,
        }
        for item in nodes or []:
            self.add_node(item.get("node_id", ""), item.get("room_id", ""), item.get("label", ""))

    @classmethod
    def from_config(cls, config: Optional[dict], now_fn: Optional[Callable[[], float]] = None) -> "NodeRegistry":
        cfg = config or {}
        return cls(
            nodes=cfg.get("nodes") or [],
            stale_after_seconds=cfg.get("stale_after_seconds", 30),
            online_after_seconds=cfg.get("online_after_seconds", 60),
            now_fn=now_fn,
        )

    # ---------- 注册 ----------
    def add_node(self, node_id: str, room_id: str, label: str = "") -> Optional[Node]:
        node_id = (node_id or "").strip()
        if not node_id:
            return None
        with self._lock:
            existing = self.nodes.get(node_id)
            if existing is not None:
                existing.room_id = room_id or existing.room_id
                existing.label = label or existing.label
                return existing
            node = Node(node_id=node_id, room_id=room_id or "", label=label or "")
            self.nodes[node_id] = node
            return node

    def get(self, node_id: str) -> Optional[Node]:
        with self._lock:
            return self.nodes.get(node_id)

    def nodes_for_room(self, room_id: str) -> list[Node]:
        with self._lock:
            return [n for n in self.nodes.values() if n.room_id == room_id]

    # ---------- 写入 ----------
    def update_motion(self, node_id: str, state: str, score=None,
                      device_ts_ms=None, received_at: Optional[float] = None) -> bool:
        """消费一条 motion/idle 事件。返回是否被接受。"""
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                self._stats["unknown_node"] += 1
                return False
            if state not in VALID_STATES:
                self._stats["rejected"] += 1
                return False

            now = float(received_at if received_at is not None else self._now())
            previous = node.state
            node.state = state
            node.score = _as_float(score)
            node.device_ts_ms = _as_int(device_ts_ms)
            node.last_seen = now
            node.last_event_at = now
            node.stale = False
            node.online = True

            if state == "motion":
                node.last_motion_at = now
                node.idle_since = None
            elif state == "idle":
                # 只有从非 idle 进入 idle 才重置起点 —— 重复 idle 不能把 300 秒清零
                if previous != "idle" or node.idle_since is None:
                    node.idle_since = now
            self._stats["motion"] += 1
            return True

    def update_liveness(self, node_id: str, received_at: Optional[float] = None) -> bool:
        """消费 health / capabilities：只证明"节点还活着"，**不**刷新 stale。"""
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                self._stats["unknown_node"] += 1
                return False
            node.last_seen = float(received_at if received_at is not None else self._now())
            node.online = True
            self._stats["liveness"] += 1
            return True

    def update_wifi(self, node_id: str, rssi_dbm=None, received_at: Optional[float] = None) -> bool:
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                self._stats["unknown_node"] += 1
                return False
            node.rssi_dbm = _as_int(rssi_dbm)
            node.last_seen = float(received_at if received_at is not None else self._now())
            node.online = True
            self._stats["wifi"] += 1
            return True

    def update_sensing(self, node_id: str, calibration_state=None,
                       received_at: Optional[float] = None) -> bool:
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                self._stats["unknown_node"] += 1
                return False
            if calibration_state is not None:
                node.calibration_state = str(calibration_state)
            node.last_seen = float(received_at if received_at is not None else self._now())
            node.online = True
            self._stats["sensing"] += 1
            return True

    # ---------- 判定 ----------
    def sweep(self, now: Optional[float] = None) -> dict:
        """按后端时钟重算 online / stale。

        必须由定时循环调用（默认 5 秒），不能等下一帧：节点掉线后不会再有消息，
        等消息就等于永远发现不了。
        """
        now = float(now if now is not None else self._now())
        changed: dict[str, dict] = {}
        with self._lock:
            for node_id, node in self.nodes.items():
                online = node.last_seen > 0 and (now - node.last_seen) <= self.online_after_seconds
                stale = node.last_event_at <= 0 or (now - node.last_event_at) > self.stale_after_seconds
                if online != node.online or stale != node.stale:
                    changed[node_id] = {"online": online, "stale": stale}
                node.online = online
                node.stale = stale
        return changed

    def states(self, now: Optional[float] = None) -> dict[str, dict]:
        """给 `fuse_rooms()` 的输入：节点 id → 脱敏状态（无原始 CSI）。"""
        now = float(now if now is not None else self._now())
        self.sweep(now)
        with self._lock:
            return {node_id: self.nodes[node_id].to_dict(now) for node_id in sorted(self.nodes)}

    def status(self, now: Optional[float] = None) -> dict:
        now = float(now if now is not None else self._now())
        self.sweep(now)
        with self._lock:
            return {
                "node_count": len(self.nodes),
                "online_count": sum(1 for n in self.nodes.values() if n.online),
                "stale_count": sum(1 for n in self.nodes.values() if n.stale),
                "nodes": [n.to_dict(now) for n in self.nodes.values()],
                "stats": dict(self._stats),
                "now": round(now, 3),
            }

    def snapshot(self) -> list[dict]:
        now = self._now()
        self.sweep(now)
        with self._lock:
            return [n.to_dict(now) for n in self.nodes.values()]


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
