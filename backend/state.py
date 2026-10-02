# -*- coding: utf-8 -*-
"""单一真相源 StateStore。

- version：状态版本号，每次成功执行设备动作后 +1（替代会话内存，支持崩溃恢复与审计）。
- audit_log：append-only 审计日志（谁在哪个版本执行了什么命令）。
- pending：高危动作待确认队列。
- trace：本轮决策链路（Ground→Propose→Verify→Execute），供前端可视化。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TraceStep:
    step: str        # ground | propose | verify | execute
    detail: str
    ok: bool = True

    def to_dict(self) -> dict:
        return {"step": self.step, "detail": self.detail, "ok": self.ok}


class StateStore:
    def __init__(self, max_log: int = 2000):
        self.version = 0
        self.audit_log: list[dict] = []
        self.pending: dict[str, dict] = {}   # pending_id -> {lease, action, text}
        self.max_log = max_log

    @property
    def _max_log(self) -> int:
        """兼容旧引用。新代码用 `max_log`。"""
        return self.max_log

    # ---------- 版本 ----------
    def bump(self) -> int:
        self.version += 1
        return self.version

    # ---------- 审计 ----------
    def record(self, lease, device_id: str, command: str, params: dict, result: dict) -> dict:
        entry = {
            "version": self.version,
            "lease_id": lease.lease_id if lease else "",
            "agent": lease.agent if lease else "",
            "source": lease.source if lease else "",
            "device_id": device_id,
            "command": command,
            "params": params,
            "ok": result.get("ok", False),
            "ts": time.time(),
        }
        self.audit_log.append(entry)
        if len(self.audit_log) > self.max_log:
            self.audit_log = self.audit_log[-self.max_log:]
        return entry

    def get_audit(self, limit: int = 50) -> list[dict]:
        return self.audit_log[-limit:]

    def audit_since(self, ts: float, limit: int = 10000) -> list[dict]:
        """取某时刻之后的审计记录，供日报/统计使用。

        调用方必须意识到**这是环形缓冲**：`audit_log` 只保留最近 `max_log` 条，
        超出的旧记录已被丢弃。「今天执行了 N 次」在 N 接近缓冲上限时是**下界**
        而不是总数，日报里必须如实标注（见 `jobs/daily_summary.collect_facts`）。
        """
        return [e for e in self.audit_log[-limit:] if float(e.get("ts", 0)) >= ts]

    # ---------- 待确认 ----------
    def add_pending(self, lease, action: dict, text: str) -> str:
        pending_id = uuid.uuid4().hex[:10]
        self.pending[pending_id] = {"lease": lease, "action": action, "text": text}
        return pending_id

    def pop_pending(self, pending_id: str) -> Optional[dict]:
        return self.pending.pop(pending_id, None)

    # ---------- 快照 ----------
    def snapshot(self) -> dict:
        return {"version": self.version, "pending_count": len(self.pending)}
