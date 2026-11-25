# -*- coding: utf-8 -*-
"""L5 自治服务：把策略、偏好带、四道闸与执行入口编排在一起。

对外只有四个动作：tick（评估+可选执行）、accept/reject（反馈）、status。
真正的执行仍走 `execution.execute_plan(source="adaptation")`。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from ..config_loader import load_adaptation
from .policy import AdaptationPolicy


class AdaptationService:
    def __init__(self, registry, store,
                 sensors_getter: Optional[Callable[[], dict]] = None,
                 rooms_getter: Optional[Callable[[], dict]] = None,
                 config: Optional[dict] = None,
                 state_path=None):
        self.registry = registry
        self.store = store
        self.sensors_getter = sensors_getter or (lambda: {})
        self.rooms_getter = rooms_getter or (lambda: {})
        self.config = config if config is not None else load_adaptation()
        self.policy = AdaptationPolicy(self.config, state_path=state_path)
        self.last_tick: float = 0.0
        self.last_result: dict = {}

    # ---------- 主循环 ----------
    def tick(self, now: Optional[float] = None) -> dict:
        now = float(now if now is not None else time.time())
        self.last_tick = now
        context = {
            "sensors": self.sensors_getter() or {},
            "rooms": self.rooms_getter() or {},
        }
        entries = self.store.get_audit(limit=500) if self.store is not None else []
        outcome = self.policy.propose(context, entries, self.registry, self.store, now)
        applied = {"ok": False, "reason": "无可执行提议", "results": []}
        if any(p.executable for p in self.policy.last_proposals):
            applied = self.policy.apply(self.policy.last_proposals, self.registry,
                                        self.store, now)
        self.last_result = {**outcome, "applied": applied, "now": now}
        return self.last_result

    # ---------- 反馈 ----------
    def accept(self, proposal_id: str, now: Optional[float] = None) -> dict:
        ok = self.policy.accept(proposal_id)
        if not ok:
            return {"ok": False, "error": "提议不存在或未通过门控"}
        result = self.policy.apply(self.policy.last_proposals, self.registry,
                                   self.store, now)
        return {"ok": result.get("ok", False), "proposal_id": proposal_id, **result}

    def reject(self, proposal_id: str) -> dict:
        ok = self.policy.reject(proposal_id)
        return {"ok": ok, **({} if ok else {"error": "提议不存在"})}

    # ---------- 视图 ----------
    def status(self) -> dict:
        metrics = self._metrics()
        return {
            **self.policy.status(),
            "metrics": metrics,
            "drift": self.policy.state.drift,
            "last_tick": round(self.last_tick, 3),
            "last_result": {
                "proposals": self.last_result.get("proposals", []),
                "applied": self.last_result.get("applied", {}),
            },
        }

    def health(self) -> dict:
        metrics = self._metrics()
        return {"level": self.policy.state.level,
                "mode": self.policy.state.mode,
                "metrics": metrics,
                "drift": self.policy.state.drift,
                "thresholds": self.config.get("drift") or {},
                "note": ("指标越限时按 auto → confirm → suggest_only → off 逐级退让；"
                         "退让后清空反馈、回影子模式重走冷启动")}

    def _metrics(self) -> dict:
        """24h 滚动指标的诚实近似：纠正率、自动执行占比、感知降级比例。

        目前样本来自内存会话（进程重启归零），报告里必须注明这一点，
        不能当作长期统计。
        """
        state = self.policy.state
        feedback = max(1, int(state.feedback))
        total = max(1, int(state.proposals_total))
        rooms = self.rooms_getter() or {}
        states = [str((r or {}).get("state", "")) for r in rooms.values()]
        degraded = sum(1 for s in states if s == "degraded")
        return {
            "correction_rate": round(state.corrections / feedback, 3),
            "auto_execution_ratio": round(state.applied / total, 3),
            "degraded_ratio": round(degraded / len(states), 3) if states else 0.0,
            "feedback": state.feedback,
            "applied": state.applied,
            "note": "会话内近似值；进程重启归零，不是长期统计",
        }

    def reload_config(self) -> dict:
        self.config = load_adaptation()
        self.policy.config = self.config
        return {"enabled": bool(self.config.get("enabled", False))}
