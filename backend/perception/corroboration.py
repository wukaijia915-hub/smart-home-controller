# -*- coding: utf-8 -*-
"""多源交叉校验：把「CSI 说没运动」提升为「房间确实无人」，或如实降为不确定。

## 为什么需要这一层

`fuse_rooms()` 只回答 CSI 一侧的问题：**有没有新鲜运动**。它刻意不判无人 ——
全部节点 idle 超过阈值时只置 `empty_candidate=True`，因为 `idle` 只表示
"没有运动"，人静坐、宠物、设备掉线都会让它成立。

本模块负责第二半：**旁证是否一致地指向无人**。只有同时满足

    ① CSI 无运动   ② 至少 N 路旁证一致指向无人   ③ 持续超过阈值

才输出 `empty_confident`。任一源冲突 → `uncertain`，并给出冲突类别
（真人 / 宠物 / 环境干扰 / 传感器故障 / 不确定）；`degraded` 永不推断无人。

## 六路旁证的诚实标注

申报书列了六路：CSI、PIR、插座功率、设备操作、作息基线、门磁。
当前阶段除 CSI 外均为**仿真量**（`source_kind="simulated"`），证据里逐项标注；
接入真实器件后只改 RoomService 的取值来源，本模块的判定规则不变。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .rooms import (DEGRADED, EMPTY_CONFIDENT, PRESENT_CONFIDENT, UNCERTAIN,
                    RoomState)

#: 冲突类别（与申报书一致）
CONFLICT_HUMAN = "human"
CONFLICT_PET = "pet"
CONFLICT_ENVIRONMENT = "environment"
CONFLICT_FAULT = "fault"
CONFLICT_UNKNOWN = "unknown"

CONFLICTS = (CONFLICT_HUMAN, CONFLICT_PET, CONFLICT_ENVIRONMENT,
             CONFLICT_FAULT, CONFLICT_UNKNOWN)


@dataclass
class SourceVote:
    source: str
    value: object
    consistent_with_empty: bool
    source_kind: str = "simulated"      # real | simulated
    note: str = ""

    def to_dict(self) -> dict:
        return {"source": self.source, "value": self.value,
                "consistent_with_empty": self.consistent_with_empty,
                "source_kind": self.source_kind, "note": self.note}


@dataclass
class CorroborationResult:
    room: RoomState
    sources: list = field(default_factory=list)
    empty_votes: int = 0
    active_votes: int = 0
    conflict: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {**self.room.to_dict(), "corroboration": {
            "sources": [s.to_dict() for s in self.sources],
            "empty_votes": self.empty_votes,
            "active_votes": self.active_votes,
            "conflict": self.conflict,
            "reason": self.reason,
        }}


def _corr_config(config: Optional[dict]) -> dict:
    cfg = config or {}
    return cfg.get("corroboration") or {}


def _recent(ts, now: float, window: float) -> bool:
    """时间窗内的事件。

    必须拒绝**未来时间戳**：回放/时钟偏移时 audit 的 ts 可能大于 now，
    若只算上界会把"未来的动作"当成正在发生，导致回放永远无法判无人。
    """
    if ts is None:
        return False
    delta = float(now) - float(ts)
    return 0.0 <= delta <= float(window)


def _routine_expected(room_id: str, config: dict, now: float) -> Optional[bool]:
    """作息基线：该房间在这个时段历史上通常是否有人。来自配置（仿真基线）。"""
    corr = _corr_config(config)
    routine = (corr.get("routine") or {}).get(room_id)
    if not routine:
        return None
    hour = time.localtime(now).tm_hour
    hours = [int(h) for h in (routine.get("present_hours") or [])]
    return hour in hours


def corroborate(room: RoomState, evidence: Optional[dict],
                config: Optional[dict] = None,
                now: Optional[float] = None) -> CorroborationResult:
    """对一间房做旁证校验。

    `evidence` 形状（全部可选，缺省按"无此证据"处理，不计票）：
        pir            {"motion": bool, "last_motion_at": ts}
        power_draw     float（瓦）
        recent_actions list[{"device_id","command","ts"}]
        door           {"open": bool, "last_open_at": ts}
        camera         {"person": bool, "ts": ts}
    """
    now = float(now if now is not None else time.time())
    cfg = config or {}
    corr = _corr_config(cfg)
    evidence = evidence or {}

    # 有人 / 降级：不进入"判无人"流程，原样返回（motion 优先；degraded 不推断）
    if room.state == PRESENT_CONFIDENT:
        return CorroborationResult(room=room, reason="motion 已确认有人，无需旁证")
    if room.state == DEGRADED:
        return CorroborationResult(room=room, reason="节点降级，禁止推断无人")

    candidate = bool((room.evidence or {}).get("empty_candidate"))
    if not candidate:
        return CorroborationResult(room=room, reason="CSI 尚未达到空闲阈值，无需旁证")

    min_sources = int(corr.get("min_sources", 2) or 2)
    pir_window = float(corr.get("pir_motion_window_seconds", 120))
    power_threshold = float(corr.get("power_active_threshold_w", 20))
    action_window = float(corr.get("action_window_seconds", 300))
    door_window = float(corr.get("door_window_seconds", 300))

    votes: list[SourceVote] = []

    # 1) PIR：近期有较大动作 → 有人；否则与无人一致
    pir = evidence.get("pir")
    if isinstance(pir, dict):
        motion = bool(pir.get("motion"))
        recent = motion or _recent(pir.get("last_motion_at"), now, pir_window)
        votes.append(SourceVote("pir", {"motion": motion, "recent": recent},
                                consistent_with_empty=not recent,
                                note="PIR 只回答近期有无较大动作"))

    # 2) 插座功率：有持续功率 → 有人在用电；低功率与无人一致
    power = evidence.get("power_draw")
    if isinstance(power, (int, float)):
        active = float(power) >= power_threshold
        votes.append(SourceVote("power_draw", {"watts": round(float(power), 1),
                                               "threshold": power_threshold},
                                consistent_with_empty=not active,
                                note="功率高于阈值视为活动证据"))

    # 3) 设备操作日志：窗口内该房间有操作 → 有人。
    #    只有调用方**显式提供**了这份日志才计票（缺省 = 没查过，不是"没人操作"）。
    actions_raw = evidence.get("recent_actions")
    if actions_raw is not None:
        actions = [a for a in actions_raw if _recent(a.get("ts"), now, action_window)]
        votes.append(SourceVote("device_actions", {"count": len(actions)},
                                consistent_with_empty=not actions,
                                note=f"{int(action_window)}s 内该房间的设备操作数"))

    # 4) 门磁：窗口内开过门 → 可能有人进出
    door = evidence.get("door")
    if isinstance(door, dict):
        opened = bool(door.get("open")) or _recent(door.get("last_open_at"), now, door_window)
        votes.append(SourceVote("door", {"open": bool(door.get("open")),
                                         "recently_opened": opened},
                                consistent_with_empty=not opened,
                                note="窗口内开门视为人员进出证据"))

    # 5) 作息基线：该时段通常有人 → 降低"无人"可信度
    expected = _routine_expected(room.room_id, cfg, now)
    if expected is not None:
        votes.append(SourceVote("routine", {"expected_present": expected},
                                consistent_with_empty=not expected,
                                note="作息基线（配置的仿真时段，非实测）"))

    # 6) 摄像头事件（可选，公共区域才可能有）
    camera = evidence.get("camera")
    if isinstance(camera, dict):
        person = bool(camera.get("person"))
        votes.append(SourceVote("camera", {"person": person},
                                consistent_with_empty=not person,
                                source_kind="simulated",
                                note="摄像头事件仅作旁证，画面本地处理"))

    empty_votes = sum(1 for v in votes if v.consistent_with_empty)
    active_votes = sum(1 for v in votes if not v.consistent_with_empty)
    conflict = _classify_conflict(votes, room, evidence)

    if active_votes > 0:
        state = UNCERTAIN
        reason = (f"CSI 无运动，但 {active_votes} 路旁证指向有人"
                  f"（冲突类别：{conflict}）；不确定状态下不执行关空调/关灯")
        confidence = min(0.6, 0.2 + 0.1 * empty_votes)
    elif empty_votes >= min_sources:
        state = EMPTY_CONFIDENT
        confidence = min(0.95, 0.5 + 0.15 * empty_votes)
        reason = f"{empty_votes} 路旁证一致指向无人（阈值 {min_sources}）"
        conflict = ""
    else:
        state = UNCERTAIN
        confidence = 0.3
        reason = (f"旁证不足：{empty_votes} 路一致，需 {min_sources} 路；"
                  f"不确定是刻意输出，不硬判无人")

    evidence_out = dict(room.evidence or {})
    evidence_out["corroboration"] = {
        "sources": [v.to_dict() for v in votes],
        "empty_votes": empty_votes,
        "active_votes": active_votes,
        "conflict": conflict,
        "reason": reason,
        "min_sources": min_sources,
        "note": "单源不得判无人；冲突不丢弃，分类归因",
    }
    promoted = RoomState(
        room_id=room.room_id, label=room.label, state=state,
        confidence=confidence, nodes=list(room.nodes),
        last_motion_at=room.last_motion_at, updated_at=now,
        evidence=evidence_out,
    )
    return CorroborationResult(room=promoted, sources=votes,
                               empty_votes=empty_votes, active_votes=active_votes,
                               conflict=conflict, reason=reason)


def _classify_conflict(votes: list[SourceVote], room: RoomState, evidence: dict) -> str:
    """把冲突分类成可归因的五类之一。无冲突返回空串。"""
    active = {v.source for v in votes if not v.consistent_with_empty}
    if not active:
        return ""

    pir = evidence.get("pir") or {}
    power = evidence.get("power_draw")
    door = evidence.get("door") or {}
    actions = evidence.get("recent_actions") or []

    pir_motion = bool(pir.get("motion")) or bool(pir.get("last_motion_at"))
    power_active = isinstance(power, (int, float)) and power >= 20
    door_recent = bool(door.get("open")) or bool(door.get("last_open_at"))
    has_actions = bool(actions)

    if pir_motion and power_active:
        return CONFLICT_HUMAN
    if pir_motion and not power_active and not has_actions:
        # 有小幅动作但没用电、没操作 → 更像宠物/环境扰动
        return CONFLICT_PET
    if power_active and not pir_motion and not door_recent:
        # 电器在跑（冰箱/路由器/待机），不是人
        return CONFLICT_ENVIRONMENT
    if door_recent and not pir_motion and not power_active:
        return CONFLICT_PET
    if has_actions:
        return CONFLICT_HUMAN
    return CONFLICT_UNKNOWN
