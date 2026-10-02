# -*- coding: utf-8 -*-
"""HomeAgent 门面：把 Root + Manager 决策链封装为对外统一接口。

对外接口 decide(text) -> dict 保持稳定，HTTP / WebSocket / 渠道接入无需大改。
内部：Ground → Root(意图理解 + 提议具体动作 + 授权) → Manager(校验 + 执行 + 记录) → 单一真相源。

一次 decide() 的模型调用数：规则命中 0 次，走慢环 1 次（Manager 不持有 LLM）。
"""
from __future__ import annotations

from .agents import RootAgent, ManagerAgent
from .devices.registry import DeviceRegistry
from .edge_cloud import DecisionRouter
from .knowledge.rag import kb
from .llm_client import LLMClient
from .skills import skill_manager
from .state import StateStore


class HomeAgent:
    def __init__(self, registry: DeviceRegistry, capabilities=None):
        self.registry = registry
        self.llm = LLMClient()
        self.store = StateStore()
        self.router = DecisionRouter()   # 端云协同路由（隐私分级 + 端/云决策）
        self.capabilities = capabilities
        # 漏斗③（技能）与漏斗④（能力契约）需要这两个注册表；
        # 不注入时对应漏斗自动跳过，不影响其余决策路径。
        self.root = RootAgent(registry, self.store, self.llm,
                              capabilities=capabilities, skills=skill_manager)
        # Manager 是纯护栏，**不持有 LLM**：Root 单次提议具体动作，Manager 只做
        # 校验/授权/执行/记录。一次 decide() 的 LLM 调用数因此为 0（规则命中）
        # 或 1（走慢环），不再是原来的 1+N。
        self.manager = ManagerAgent(registry, self.store, capabilities=capabilities)
        self.session: list[dict] = []   # 短期会话记忆
        self.history: list[dict] = []   # 完整历史（界面展示用）
        self.sensors: dict = {}         # 当前传感器数据（main.py 注入）

    def decide(self, text: str) -> dict:
        # 每次决策重置慢环预算（次数/墙钟），避免跨请求累积
        self.root.budget.begin()

        # 端云协同路由：隐私分级 → 决定「端侧本地处理」还是「允许上云」
        route = self.router.route(text, self.sensors)
        cloud_text = self.router.guard.sanitize(text) if route.need_sanitize else None

        plan = self.root.plan(text, self.sensors, self.session,
                              allow_cloud=route.cloud_allowed, cloud_text=cloud_text)

        # 上云审计：仅当实际调用云侧大模型（plan.source == "llm"）时记录数据最小化
        if plan.source == "llm":
            self.router.guard.record_upload(route.level, text, cloud_text or text)

        # 无操作意图（寒暄 / 兜底 / LLM 判断 is_command=false）：不执行任何设备
        if not plan.is_command or not plan.sub_tasks:
            self._remember(text, plan.reply)
            trace = [{"step": "propose", "detail": "无操作意图，未执行任何设备", "ok": True}]
            return {
                "reply": plan.reply,
                "actions": [],
                "results": [],
                "digital_human": {"emotion": "neutral", "action": "speak"},
                "decision_trace": trace,
                "edge_cloud": route.to_dict(),
                "requires_confirmation": False,
                "pending_id": "",
                "pending_action": None,
                "llm_budget": self._budget_view(trace),
            }

        report = self.manager.run(plan, self.session)

        # RAG：记录用户操作 / 场景习惯
        for r in report.results:
            if r.get("ok"):
                kb.record_command(r.get("device_id", ""), r.get("command", ""), r.get("params", {}))
        scene = self.root._match_scene(text)
        if scene:
            kb.record_scene(scene)

        self._remember(text, report.reply)

        trace = [t.to_dict() for t in report.trace]
        return {
            "reply": report.reply,
            "actions": report.actions,
            "results": report.results,
            "digital_human": {"emotion": "happy" if report.actions else "neutral", "action": "speak"},
            "decision_trace": trace,
            "edge_cloud": route.to_dict(),
            "requires_confirmation": report.requires_confirmation,
            "pending_id": report.pending_id,
            "pending_action": report.pending_action,
            "llm_budget": self._budget_view(trace),
        }

    def _budget_view(self, trace: list) -> dict:
        """把本次决策的模型预算用量并入返回体。

        降级必须**可见**：静默超时/熔断与正常工作在返回体上长得一模一样，
        只有把 degraded 标出来才能在演示现场和事后审计里看出差异。
        """
        snap = self.root.budget.snapshot()
        if snap.get("degraded"):
            trace.append({"step": "degrade",
                          "detail": f"慢环降级：{snap.get('reason', '')}",
                          "ok": False})
        return snap

    def confirm(self, pending_id: str) -> dict:
        """高危操作二次确认后执行。"""
        result = self.manager.execute_pending(pending_id)
        if result.get("ok"):
            kb.record_command(result.get("device_id", ""), result.get("command", ""), result.get("params", {}))
        return result

    def _remember(self, text: str, reply: str):
        self.session.append({"role": "user", "content": text})
        self.session.append({"role": "assistant", "content": reply})
        if len(self.session) > 20:
            self.session = self.session[-20:]
