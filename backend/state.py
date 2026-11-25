# -*- coding: utf-8 -*-
"""单一真相源 StateStore。

- version：状态版本号，每次成功执行设备动作后 +1（替代会话内存，支持崩溃恢复与审计）。
- audit_log：append-only 审计日志（谁在哪个版本执行了什么命令，含前态/后态/回读/失败层）。
- decisions：决策链（signal → inference → gate → lease → action → readback），按 decision_id 索引。
- pending：高危动作待确认队列。
- 执行闸状态：last_action_at（每设备上次动作时刻）与 daily_counts（当日动作计数），
  供参数包络之外的「频率限制 / 当日预算」两道闸读取。
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
    def __init__(self, max_log: int = 2000, max_decisions: int = 200):
        self.version = 0
        self.audit_log: list[dict] = []
        self.pending: dict[str, dict] = {}   # pending_id -> {lease, action, text}
        self.max_log = max_log
        # 决策链：decision_id -> {"decision_id","source","failed_layer","stages":[...]}
        self.decisions: dict[str, dict] = {}
        self.max_decisions = max_decisions
        # 单调序号：同一微秒内连续创建的决策也要能区分先后（trim/recent 的 tie-break）
        self._decision_seq = 0
        # 执行闸状态（频率 / 当日预算）
        self.last_action_at: dict[str, float] = {}
        self.daily_counts: dict[str, int] = {}

    @property
    def _max_log(self) -> int:
        """兼容旧引用。新代码用 `max_log`。"""
        return self.max_log

    # ---------- 版本 ----------
    def bump(self) -> int:
        self.version += 1
        return self.version

    # ---------- 审计 ----------
    def record(self, lease, device_id: str, command: str, params: dict, result: dict,
               version_after: Optional[int] = None, decision_id: str = "",
               before: Optional[dict] = None, after: Optional[dict] = None,
               failed_layer: str = "", action_id: str = "", source: str = "") -> dict:
        """写一条审计。

        除了「谁在什么版本执行了什么」，还固化**执行结局、前态、后态与回读证据**：
        - status：confirmed / delivered / failed / error（三态证据链的入口）
        - before / after：动作前后设备属性快照（撤销与"是否漂移"的依据）
        - readback：回读属性、期望值、实际值、可信与否、原因
        - decision_id：把这条动作挂到六段因果链上
        - failed_layer：失败归因到哪一层（gate/lease/device/readback）

        存进审计而不是只返回给调用方，是因为**事后复盘看的是审计**：
        若证据只活在 HTTP 响应里，一次页面刷新就没了。
        """
        entry = {
            "action_id": action_id,
            "decision_id": decision_id,
            "version": self.version,
            "version_after": self.version if version_after is None else version_after,
            "lease_id": lease.lease_id if lease else "",
            "agent": lease.agent if lease else "",
            "source": lease.source if lease else source,
            "device_id": device_id,
            "command": command,
            "params": params,
            "before": before or {},
            "after": after or {},
            "ok": result.get("ok", False),
            "status": result.get("status", ""),
            "readback": result.get("readback"),
            "failed_layer": failed_layer,
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

    # ---------- 决策链（六段因果） ----------
    def open_decision(self, decision_id: str, source: str,
                      causal: Optional[dict] = None) -> dict:
        """建一条决策链。causal 可带 signal / inference / gate 三段。"""
        causal = causal or {}
        self._decision_seq += 1
        chain = {
            "decision_id": decision_id,
            "source": source,
            "created_at": time.time(),
            "seq": self._decision_seq,
            "failed_layer": "",
            "stages": [],
        }
        for name in ("signal", "inference", "gate"):
            if causal.get(name) is not None:
                chain["stages"].append({"stage": name, "detail": causal[name]})
        self.decisions[decision_id] = chain
        self._trim_decisions()
        return chain

    def append_stage(self, decision_id: str, stage: str, detail) -> None:
        chain = self.decisions.get(decision_id)
        if chain is None:
            return
        chain["stages"].append({"stage": stage, "detail": detail, "ts": round(time.time(), 3)})
        if stage == "readback" and isinstance(detail, dict) and detail.get("failed_layer"):
            chain["failed_layer"] = str(detail["failed_layer"])

    def set_failed_layer(self, decision_id: str, layer: str) -> None:
        chain = self.decisions.get(decision_id)
        if chain is not None and layer:
            chain["failed_layer"] = layer

    def get_decision(self, decision_id: str) -> Optional[dict]:
        return self.decisions.get(decision_id)

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        items = sorted(self.decisions.values(),
                       key=lambda c: (c.get("created_at", 0), c.get("seq", 0)),
                       reverse=True)
        return items[:max(1, int(limit))]

    def _trim_decisions(self) -> None:
        if len(self.decisions) <= self.max_decisions:
            return
        keep = sorted(self.decisions.values(),
                      key=lambda c: (c.get("created_at", 0), c.get("seq", 0)),
                      reverse=True)[:self.max_decisions]
        self.decisions = {c["decision_id"]: c for c in keep}

    # ---------- 执行闸状态 ----------
    def note_action(self, device_id: str, now: Optional[float] = None) -> None:
        """记录一次动作：更新每设备上次动作时刻与当日计数（频率/预算闸用）。"""
        now = float(now if now is not None else time.time())
        self.last_action_at[device_id] = now
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        self.daily_counts[day] = self.daily_counts.get(day, 0) + 1

    def actions_today(self, now: Optional[float] = None) -> int:
        now = float(now if now is not None else time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        return int(self.daily_counts.get(day, 0))

    def seconds_since_action(self, device_id: str, now: Optional[float] = None) -> Optional[float]:
        last = self.last_action_at.get(device_id)
        if last is None:
            return None
        now = float(now if now is not None else time.time())
        return max(0.0, now - last)

    # ---------- 待确认 ----------
    def add_pending(self, lease, action: dict, text: str) -> str:
        pending_id = uuid.uuid4().hex[:10]
        self.pending[pending_id] = {"lease": lease, "action": action, "text": text}
        return pending_id

    def pop_pending(self, pending_id: str) -> Optional[dict]:
        return self.pending.pop(pending_id, None)

    # ---------- 快照 ----------
    def snapshot(self) -> dict:
        return {"version": self.version, "pending_count": len(self.pending),
                "decisions": len(self.decisions), "actions_today": self.actions_today()}
