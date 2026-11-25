# -*- coding: utf-8 -*-
"""房间四态融合：节点状态 → 房间级结论（CSI 单源，仅做初步判定）。

## 状态定义

| 状态 | 含义 | 允许的动作 |
|---|---|---|
| `present_confident` | 有新鲜运动事件 | 可逆动作 |
| `empty_confident` | **本模块不产出**；必须由 `corroboration.corroborate()` 在旁证一致后提升 | 可逆动作 |
| `uncertain` | 数据不足、冲突、或"idle 够久但旁证未到" | 只做低危动作 |
| `degraded` | 节点掉线 / 无数据 | 禁止推断无人，只降置信度并告警 |

## 两条不能违反的规则

1. **只用二值 `state`，不比较跨节点 `score`** —— score 跨节点不可比。
2. **单源不得判无人** —— 全部节点 idle 超过阈值时只置 `empty_candidate=True`，
   状态仍是 `uncertain`，等 `corroborate()` 补齐旁证。这是整个项目最核心的语义缺口。

`fuse_rooms()` 是纯函数：给定相同输入与 `now`，结果完全一致，便于回放与测试。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

PRESENT_CONFIDENT = "present_confident"
EMPTY_CONFIDENT = "empty_confident"
UNCERTAIN = "uncertain"
DEGRADED = "degraded"

ALL_STATES = (PRESENT_CONFIDENT, EMPTY_CONFIDENT, UNCERTAIN, DEGRADED)

#: `fuse_rooms()` 可能产出的状态（empty 必须由 corroborate 提升）
FUSION_STATES = (PRESENT_CONFIDENT, UNCERTAIN, DEGRADED)


@dataclass
class RoomState:
    room_id: str
    label: str
    state: str
    confidence: float
    nodes: list[str] = field(default_factory=list)
    last_motion_at: Optional[float] = None
    evidence: dict = field(default_factory=dict)
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        """脱敏视图：只有派生状态，没有原始 CSI / 画面 / 音频。"""
        return {
            "room_id": self.room_id,
            "label": self.label,
            "state": self.state,
            "confidence": round(float(self.confidence), 3),
            "nodes": list(self.nodes),
            "last_motion_at": round(self.last_motion_at, 3) if self.last_motion_at else None,
            "updated_at": round(float(self.updated_at), 3),
            "evidence": self.evidence,
        }


class RoomTimeline:
    """房间状态变化的时间线（只在状态变化时追加，避免每 2 秒刷屏）。

    去重必须**按房间**看"该房间上一次"的状态，而不是全局最后一条：
    三个房间每 5 秒各追加一次时，全局最后一条永远是别的房间，
    按全局去重会让每个房间每轮都记一条 `uncertain`，时间线被灌满。
    """

    def __init__(self, max_entries: int = 500):
        self._entries: deque[dict] = deque(maxlen=max_entries)

    def append(self, room: RoomState) -> bool:
        for entry in reversed(self._entries):
            if entry["room_id"] == room.room_id:
                if entry["state"] == room.state:
                    return False
                break
        self._entries.append({
            "ts": round(float(room.updated_at), 3),
            "room_id": room.room_id,
            "label": room.label,
            "state": room.state,
            "confidence": round(float(room.confidence), 3),
            "reason": (room.evidence or {}).get("reason", ""),
        })
        return True

    def recent(self, room_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        items = [e for e in self._entries if room_id is None or e["room_id"] == room_id]
        return items[-max(1, int(limit)):]

    def __len__(self) -> int:
        return len(self._entries)


def fuse_rooms(node_states: Optional[dict], config: Optional[dict],
               now: Optional[float] = None) -> dict[str, RoomState]:
    """把节点状态融合成房间级初步状态。

    参数
    ----
    node_states: `{node_id: {"state", "stale", "online", "idle_seconds", "last_motion_at", ...}}`
                 来自 `NodeRegistry.states()`；缺失的节点按"无数据"处理。
    config:      `room_fusion.json` 的内容。
    now:         评估时刻（秒）；显式传入以保证可复现，默认取当前时间。
    """
    now = float(now if now is not None else time.time())
    cfg = config or {}
    rooms = cfg.get("rooms") or []
    rules = cfg.get("rules") or {}
    weights = cfg.get("confidence_weights") or {}
    threshold = float(rules.get("empty_requires_all_nodes_idle_seconds", 300))
    recency_window = float(cfg.get("motion_recency_window_seconds", 120))
    degraded_on_stale = bool(rules.get("degraded_on_node_stale", True))
    motion_overrides = bool(rules.get("motion_overrides_idle", True))
    uncertain_on_conflict = bool(rules.get("uncertain_on_conflict", True))

    states = node_states or {}
    result: dict[str, RoomState] = {}

    for room in rooms:
        room_id = str(room.get("room_id") or "")
        if not room_id:
            continue
        label = str(room.get("label") or room_id)
        node_ids = _unique([str(n) for n in (room.get("nodes") or [])])
        entries = {nid: states.get(nid) for nid in node_ids}

        if not node_ids:
            result[room_id] = _degraded(room_id, label, node_ids, now, "no_nodes",
                                        "房间没有配置节点", weights)
            continue

        fresh = [nid for nid in node_ids if _is_fresh(entries.get(nid))]
        stale = [nid for nid in node_ids if nid not in fresh]
        motion = [nid for nid in fresh if _state_of(entries, nid) == "motion"]
        idle = [nid for nid in fresh if _state_of(entries, nid) == "idle"]
        unknown = [nid for nid in fresh if _state_of(entries, nid) not in ("motion", "idle")]

        last_motion = _max_last_motion(entries, fresh)
        node_view = {
            nid: {
                "state": _state_of(entries, nid),
                "stale": not _is_fresh(entries.get(nid)),
                "online": bool((entries.get(nid) or {}).get("online", False)),
                "idle_seconds": _rounded((entries.get(nid) or {}).get("idle_seconds")),
            }
            for nid in node_ids
        }

        # 1) 有新鲜运动：房间有人。正向证据优先，个别节点掉线不推翻它。
        if motion and (motion_overrides or not uncertain_on_conflict):
            conf = _confidence(weights, len(fresh) / len(node_ids),
                               _recency(now, last_motion, recency_window), None)
            result[room_id] = RoomState(
                room_id=room_id, label=label, state=PRESENT_CONFIDENT,
                confidence=conf, nodes=node_ids, last_motion_at=last_motion,
                updated_at=now,
                evidence=_evidence("motion_detected", node_view, fresh, stale, threshold,
                                   False, None, weights, rules,
                                   note="新鲜 motion 事件；score 不跨节点比较"),
            )
            continue

        # 1b) 配置要求"运动优先于 idle"关闭时，motion + idle 并存按冲突处理。
        if motion and not motion_overrides:
            conf = _confidence(weights, len(fresh) / len(node_ids), 0.0, None)
            result[room_id] = RoomState(
                room_id=room_id, label=label, state=UNCERTAIN, confidence=conf,
                nodes=node_ids, last_motion_at=last_motion, updated_at=now,
                evidence=_evidence("motion_idle_conflict", node_view, fresh, stale, threshold,
                                   False, None, weights, rules,
                                   note="关闭了 motion_overrides_idle，且节点间运动/静止冲突"),
            )
            continue

        # 2) 没有新鲜事件：节点掉线或数据从未到达 → 降级，绝不推断无人。
        if not fresh:
            reason = "all_nodes_stale" if node_ids else "no_fresh_events"
            if degraded_on_stale:
                result[room_id] = _degraded(room_id, label, node_ids, now, reason,
                                            "节点无新鲜事件，禁止推断无人", weights,
                                            node_view=node_view, threshold=threshold, rules=rules)
            else:
                result[room_id] = RoomState(
                    room_id=room_id, label=label, state=UNCERTAIN, confidence=0.0,
                    nodes=node_ids, last_motion_at=None, updated_at=now,
                    evidence=_evidence(reason, node_view, fresh, stale, threshold,
                                       False, None, weights, rules,
                                       note="配置关闭了 degraded_on_node_stale，按不确定处理"),
                )
            continue

        # 3) 有新鲜节点但状态未知：不能下结论。
        if unknown:
            conf = _confidence(weights, len(fresh) / len(node_ids),
                               _recency(now, last_motion, recency_window), None)
            result[room_id] = RoomState(
                room_id=room_id, label=label, state=UNCERTAIN, confidence=conf,
                nodes=node_ids, last_motion_at=last_motion, updated_at=now,
                evidence=_evidence("unknown_state", node_view, fresh, stale, threshold,
                                   False, None, weights, rules,
                                   note="存在未知状态的节点"),
            )
            continue

        # 4) 有新鲜 idle，但还有节点没新鲜数据：覆盖不全，不能判无人。
        if len(fresh) < len(node_ids):
            conf = _confidence(weights, len(fresh) / len(node_ids), 0.0, None)
            result[room_id] = RoomState(
                room_id=room_id, label=label, state=UNCERTAIN, confidence=conf,
                nodes=node_ids, last_motion_at=last_motion, updated_at=now,
                evidence=_evidence("partial_coverage", node_view, fresh, stale, threshold,
                                   False, None, weights, rules,
                                   note="部分节点无新鲜事件，不能判无人"),
            )
            continue

        # 5) 全部节点新鲜且 idle：算持续时长，够久只置候选，不判无人。
        idle_seconds = _min_idle(entries, idle)
        candidate = idle_seconds is not None and idle_seconds >= threshold
        reason = "empty_candidate_pending_corroboration" if candidate else "idle_below_threshold"
        conf = _confidence(weights, 1.0, 0.0, None)
        result[room_id] = RoomState(
            room_id=room_id, label=label, state=UNCERTAIN, confidence=conf,
            nodes=node_ids, last_motion_at=last_motion, updated_at=now,
            evidence=_evidence(reason, node_view, fresh, stale, threshold,
                               candidate, idle_seconds, weights, rules,
                               note="idle ≠ empty；empty_confident 只能由旁证校验提升"),
        )

    return result


# ---------------------------------------------------------------- helpers
def _unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _state_of(entries: dict, node_id: str) -> str:
    entry = entries.get(node_id) or {}
    return entry.get("state", "unknown")


def _is_fresh(entry: Optional[dict]) -> bool:
    return bool(entry) and not bool(entry.get("stale", True))


def _max_last_motion(entries: dict, fresh: list[str]) -> Optional[float]:
    values = [((entries.get(nid) or {}).get("last_motion_at")) for nid in fresh]
    values = [v for v in values if v]
    return max(values) if values else None


def _min_idle(entries: dict, idle: list[str]) -> Optional[float]:
    if not idle:
        return None
    values = []
    for nid in idle:
        value = (entries.get(nid) or {}).get("idle_seconds")
        if value is None:
            return 0.0  # 无法确认持续时长，按 0 处理（宁可 uncertain）
        values.append(float(value))
    return min(values) if values else None


def _recency(now: float, last_motion_at: Optional[float], window: float) -> float:
    if not last_motion_at or window <= 0:
        return 0.0
    age = max(0.0, now - float(last_motion_at))
    return max(0.0, min(1.0, 1.0 - age / window))


def _confidence(weights: dict, online_ratio: float, recency: float,
                corroboration: Optional[float]) -> float:
    """按可用证据加权；缺失的旁证不按 0 计算，而是重新归一化。"""
    parts = [
        (float(weights.get("node_online", 0.3)), max(0.0, min(1.0, online_ratio))),
        (float(weights.get("motion_recency", 0.4)), max(0.0, min(1.0, recency))),
    ]
    if corroboration is not None:
        parts.append((float(weights.get("corroboration", 0.3)),
                      max(0.0, min(1.0, corroboration))))
    total = sum(w for w, _ in parts)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, sum(w * v for w, v in parts) / total))


def _rounded(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _degraded(room_id: str, label: str, node_ids: list[str], now: float,
              reason: str, note: str, weights: dict,
              node_view: Optional[dict] = None, threshold: float = 300,
              rules: Optional[dict] = None) -> RoomState:
    return RoomState(
        room_id=room_id, label=label, state=DEGRADED, confidence=0.0,
        nodes=node_ids, last_motion_at=None, updated_at=now,
        evidence=_evidence(reason, node_view or {}, [], node_ids, threshold,
                           False, None, weights, rules or {}, note=note),
    )


def _evidence(reason: str, node_view: dict, fresh: list[str], stale: list[str],
              threshold: float, empty_candidate: bool, idle_seconds: Optional[float],
              weights: dict, rules: dict, note: str = "") -> dict:
    return {
        "reason": reason,
        "nodes": node_view,
        "fresh_nodes": sorted(fresh),
        "stale_nodes": sorted(stale),
        "empty_candidate": bool(empty_candidate),
        "idle_seconds": _rounded(idle_seconds),
        "required_seconds": threshold,
        "min_corroborating_sources": rules.get("empty_requires_min_corroborating_sources", 2),
        "confidence_weights": dict(weights),
        "note": note,
    }
