# -*- coding: utf-8 -*-
"""撤销层：前态快照 / 逆动作 / 窗口与漂移校验（**只算不执行**）。

## 为什么撤销不放在 trust/ 里执行

设计书要求 `tests_perception.py` 扫描 `backend/trust/`，禁止出现 `registry.control(`。
但撤销本质上要"把设备改回去"。因此本模块只做四件事：

    查窗口 → 校验漂移 → 生成逆动作 → 写 UndoRecord

真正的执行由 `apply_undo()` 调用 `execution.execute_plan(source="undo")` 完成 ——
执行入口仍然只有 `backend/execution.py` 一个。这样红线扫描成立，审计也能记成
`before → after → undo` 三段，而不是"撤销动作来历不明"。

## 漂移校验（设计书没写，但必须有）

撤销前必须确认设备**当前值仍等于原动作的 after**。若用户已经手动改过，
拒绝撤销并说明原因 —— 否则撤销会覆盖别人的操作，把"恢复"变成"二次破坏"。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from ..config_loader import load_trust
from ..lease import LeaseValidator


def snapshot_attributes(device) -> dict:
    """动作前/后的属性快照。

    覆盖 number / bool / enum / text 四类：撤销 `set_mode` 需要 enum 原值，
    撤销 `play_music` 需要 text 原值。快照只存在内存审计与撤销记录里，
    不落原始 CSI / 画面 / 音频。
    """
    out = {}
    for name, attr in getattr(device, "attributes", {}).items():
        value = getattr(attr, "value", None)
        if isinstance(value, (bool, int, float, str)) or value is None:
            out[name] = value
    return out


def _changed_keys(before: dict, after: dict) -> list[str]:
    keys = set(before) | set(after)
    return [k for k in sorted(keys) if before.get(k) != after.get(k)]


def _is_irreversible(device, command: str) -> bool:
    """不可逆 / 高危动作不可撤销。清单与 trust.json 同源，不另写一份。"""
    try:
        cfg = load_trust() or {}
    except Exception:
        cfg = {}
    for item in cfg.get("irreversible", []) or []:
        if item.get("category") == getattr(device, "category", "") and \
                command in (item.get("commands") or []):
            return True
    return LeaseValidator.is_high_risk(getattr(device, "category", ""), command)


@dataclass
class UndoRecord:
    action_id: str
    decision_id: str
    device_id: str
    device_name: str
    category: str
    room: str
    command: str
    params: dict
    before: dict
    after: dict
    status: str
    source: str
    ts: float
    undone: bool = False
    undo_decision_id: str = ""

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "decision_id": self.decision_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "category": self.category,
            "room": self.room,
            "command": self.command,
            "params": self.params,
            "before": self.before,
            "after": self.after,
            "changed": _changed_keys(self.before, self.after),
            "status": self.status,
            "source": self.source,
            "ts": round(self.ts, 3),
            "undone": self.undone,
            "undo_decision_id": self.undo_decision_id,
        }


class UndoStore:
    """内存撤销记录（最近 300 条）。撤销窗口由 trust.json 决定（默认 600s）。"""

    def __init__(self, max_records: int = 300):
        self.max_records = max_records
        self._records: dict[str, UndoRecord] = {}
        self._order: list[str] = []

    def register(self, *, action_id: str, decision_id: str, device, command: str,
                 params: dict, before: dict, after: dict, status: str,
                 source: str, ts: Optional[float] = None) -> Optional[UndoRecord]:
        if not action_id or device is None:
            return None
        rec = UndoRecord(
            action_id=action_id,
            decision_id=decision_id,
            device_id=getattr(device, "device_id", ""),
            device_name=getattr(device, "name", ""),
            category=getattr(device, "category", ""),
            room=getattr(device, "room", ""),
            command=command,
            params=dict(params or {}),
            before=dict(before or {}),
            after=dict(after or {}),
            status=status,
            source=source,
            ts=float(ts if ts is not None else time.time()),
        )
        self._records[action_id] = rec
        self._order.append(action_id)
        while len(self._order) > self.max_records:
            old = self._order.pop(0)
            self._records.pop(old, None)
        return rec

    def get(self, action_id: str) -> Optional[UndoRecord]:
        return self._records.get(action_id)

    def recent(self, limit: int = 50) -> list[dict]:
        ids = [i for i in reversed(self._order) if i in self._records]
        return [self._records[i].to_dict() for i in ids[:max(1, int(limit))]]

    def clear(self) -> None:
        self._records.clear()
        self._order.clear()


undo_store = UndoStore()


def inverse_actions(record: UndoRecord) -> list[dict]:
    """由前态生成逆动作序列（最多两步：先恢复属性，再恢复开关）。"""
    before, after = record.before, record.after
    changed = set(_changed_keys(before, after))
    if not changed:
        return []

    def act(command: str, **params) -> dict:
        return {"device_id": record.device_id, "command": command, "params": params}

    def restore_power(actions: list[dict]) -> list[dict]:
        """属性恢复动作会把 on 置 True；若原态是关，补一条 turn_off。"""
        if before.get("on") is False and after.get("on") is True:
            actions.append(act("turn_off"))
        return actions

    cmd = record.command
    if cmd == "turn_on":
        return [act("turn_on")] if before.get("on") is True else [act("turn_off")]
    if cmd == "turn_off":
        return [act("turn_on")] if before.get("on") is True else [act("turn_off")]
    if cmd in ("set_brightness", "set_color_temp"):
        key = "brightness" if cmd == "set_brightness" else "color_temp"
        return restore_power([act(cmd, **{key: before.get(key)})])
    if cmd in ("open", "close", "set_position"):
        return [act("set_position", position=before.get("position", 0))]
    if cmd == "set_temperature":
        return restore_power([act("set_temperature", temperature=before.get("temperature"))])
    if cmd == "set_mode":
        return restore_power([act("set_mode", mode=before.get("mode"))])
    if cmd == "set_fan_speed":
        return restore_power([act("set_fan_speed", fan_speed=before.get("fan_speed"))])
    if cmd == "set_speed":
        return restore_power([act("set_speed", speed=before.get("speed"))])
    if cmd == "set_target":
        return restore_power([act("set_target", target=before.get("target"))])
    if cmd == "set_volume":
        return restore_power([act("set_volume", volume=before.get("volume"))])
    if cmd in ("play_music", "stop_music"):
        if before.get("playing") is True:
            return [act("play_music", track=before.get("track", ""))]
        return [act("stop_music")]
    if cmd == "set_clean_mode":
        return [act("set_clean_mode", mode=before.get("clean_mode"))]
    return []


def can_undo(action_id: str, registry, now: Optional[float] = None) -> dict:
    """撤销前置检查。返回 {ok, reason, remaining_seconds, inverse, record}。"""
    now = float(now if now is not None else time.time())
    rec = undo_store.get(action_id)
    if rec is None:
        return {"ok": False, "reason": "撤销记录不存在或已被挤出缓冲"}
    if rec.undone:
        return {"ok": False, "reason": "该动作已撤销过", "record": rec.to_dict()}

    cfg = load_trust() or {}
    window = float(cfg.get("undo_window_seconds", 600) or 600)
    elapsed = now - rec.ts
    remaining = max(0.0, window - elapsed)
    if remaining <= 0:
        return {"ok": False, "reason": f"已超过 {int(window)} 秒撤销窗口",
                "remaining_seconds": 0, "record": rec.to_dict()}

    device = registry.get(rec.device_id)
    if device is None:
        return {"ok": False, "reason": f"设备 {rec.device_id} 不存在，无法撤销",
                "remaining_seconds": round(remaining, 1), "record": rec.to_dict()}
    if _is_irreversible(device, rec.command):
        return {"ok": False, "reason": "高危/不可逆动作不提供撤销（门锁、燃气、摄像头直播等）",
                "remaining_seconds": round(remaining, 1), "record": rec.to_dict()}
    if rec.status not in ("confirmed", "delivered"):
        return {"ok": False, "reason": f"原动作状态为 {rec.status or 'unknown'}，没有可撤销的前态",
                "remaining_seconds": round(remaining, 1), "record": rec.to_dict()}

    # 漂移校验：当前值必须仍等于 after，否则说明有人改过，拒绝覆盖
    current = snapshot_attributes(device)
    changed = _changed_keys(rec.before, rec.after)
    drift = [k for k in changed if current.get(k) != rec.after.get(k)]
    if drift:
        return {"ok": False,
                "reason": f"设备当前值已被改动（{', '.join(drift)}），拒绝覆盖他人操作",
                "remaining_seconds": round(remaining, 1), "record": rec.to_dict(),
                "drift": drift}

    actions = inverse_actions(rec)
    if not actions:
        return {"ok": False, "reason": "该命令没有可逆的动作映射",
                "remaining_seconds": round(remaining, 1), "record": rec.to_dict()}
    return {"ok": True, "reason": "ok", "remaining_seconds": round(remaining, 1),
            "inverse": actions, "record": rec.to_dict()}


def apply_undo(action_id: str, *, registry, store, source: str = "undo",
               now: Optional[float] = None) -> dict:
    """执行撤销：先检查，再走 `execution.execute_plan(source="undo")`。"""
    check = can_undo(action_id, registry, now=now)
    if not check.get("ok"):
        return {"ok": False, "error": check.get("reason"), **check}

    # 延迟导入：execution 依赖本模块的 snapshot/undo_store，顶层互相 import 会成环
    from ..execution import execute_plan

    causal = {
        "signal": {"source": "undo", "action_id": action_id,
                   "decision_id": check["record"]["decision_id"]},
        "inference": {"inverse": check["inverse"],
                      "changed": check["record"]["changed"]},
        "gate": {"drift_check": "ok", "window_remaining_seconds": check["remaining_seconds"]},
    }
    outcome = execute_plan(check["inverse"], registry=registry, store=store,
                           source=source, causal=causal, now=now)
    rec = undo_store.get(action_id)
    if rec is not None:
        rec.undone = True
        rec.undo_decision_id = outcome.get("decision_id", "")
    return {"ok": outcome.get("ok", False), "action_id": action_id,
            "inverse": check["inverse"], **outcome}
