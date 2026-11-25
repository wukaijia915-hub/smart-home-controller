# -*- coding: utf-8 -*-
"""HomeAgent 门面：把 Root + Manager 决策链封装为对外统一接口。

对外接口 decide(text) -> dict 保持稳定，HTTP / WebSocket / 渠道接入无需大改。
内部：Ground → Root(意图理解 + 提议具体动作 + 授权) → Manager(校验 + 执行 + 记录) → 单一真相源。

一次 decide() 的模型调用数：规则命中 0 次，走慢环 1 次（Manager 不持有 LLM）。
"""
from __future__ import annotations

import time

from .agents import RootAgent, ManagerAgent
from .devices.registry import DeviceRegistry
from .edge_cloud import DecisionRouter
from .knowledge.rag import kb
from .llm_client import LLMClient
from .memory.experience import derive_rating, experience
from .memory.habits import habits
from .observability.llm_log import llm_log
from .skills import skill_manager
from .state import StateStore
from .toolkit.scoping import describe_scope
from .verification import apply_suffix, check_claims
from .verification.claim_checker import load_claim_settings


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
        t_start = time.perf_counter()
        # 每次决策重置慢环预算（次数/墙钟），避免跨请求累积
        self.root.budget.begin()

        # 端云协同路由：隐私分级 → 决定「端侧本地处理」还是「允许上云」
        route = self.router.route(text, self.sensors)
        cloud_text = self.router.guard.sanitize(text) if route.need_sanitize else None

        t_plan = time.perf_counter()
        plan = self.root.plan(text, self.sensors, self.session,
                              allow_cloud=route.cloud_allowed, cloud_text=cloud_text)
        plan_ms = (time.perf_counter() - t_plan) * 1000.0     # 漏斗 ①~⑤ 纯解析

        # 显式偏好登记：用户说「我喜欢 26 度」这类话时记下来，用于后续
        # 「声明 vs 实际行为」的一致性校验（见 memory/habits.py）。抽不到就什么都不做。
        habits.record_declared(text)

        # 上云审计：仅当实际调用云侧大模型（plan.source == "llm"）时记录数据最小化
        if plan.source == "llm":
            self.router.guard.record_upload(route.level, text, cloud_text or text)

        # 无操作意图（寒暄 / 兜底 / LLM 判断 is_command=false / 条件句草稿）：
        # 不执行任何设备。但**仍要核对回复里的声明** —— 模型完全可以一个动作都
        # 没提议，却在正文里说"已为您打开客厅灯"。
        if not plan.is_command or not plan.sub_tasks:
            # 草稿入箱：放在门面层而不是路由层，于是 HTTP/WS/webhook/MQTT 所有
            # 渠道都拿到同一个可确认的 draft_id（不存在"某个通道的草稿点不了确认"）。
            draft_info = None
            reply = plan.reply
            if plan.draft:
                from .drafts import draft_store
                stored = draft_store.create(payload=plan.draft,
                                            summary=plan.draft.get("desc", ""))
                if stored is None:
                    reply = reply + " 不过草稿箱已满，请先确认或取消已有草稿后再试。"
                else:
                    draft_info = stored.to_dict()

            claim = check_claims(reply, [], [], self._device_meta())
            reply = apply_suffix(reply, claim, load_claim_settings()) if claim.issues else reply
            self._remember(text, reply)
            trace = [{"step": "propose", "detail": "无操作意图，未执行任何设备", "ok": True}]
            if draft_info:
                trace.append({"step": "draft",
                              "detail": (f"生成规则草稿 {draft_info['draft_id']}（未生效）："
                                         f"{draft_info['summary']}"),
                              "ok": True})
            elif plan.draft:
                trace.append({"step": "draft", "detail": "草稿箱已满，未入箱", "ok": False})
            if claim.issues:
                trace.append({"step": "claim", "detail": "声明校验：" + claim.detail(), "ok": False})
            self._record_metrics(plan, trace, plan_ms, t_start)
            return {
                "reply": reply,
                "actions": [],
                "results": [],
                "verification": {"total": 0, "confirmed": 0, "delivered": 0,
                                 "failed": 0, "error": 0, "rejected": 0,
                                 "all_confirmed": False, "unverified": []},
                "claim_issues": [i.to_dict() for i in claim.issues],
                "scope": plan.scope or {},
                "draft": draft_info,
                "draft_id": draft_info["draft_id"] if draft_info else "",
                "digital_human": {"emotion": "neutral", "action": "speak"},
                "decision_trace": trace,
                "edge_cloud": route.to_dict(),
                "requires_confirmation": False,
                "pending_id": "",
                "pending_action": None,
                "llm_budget": self._budget_view(trace),
            }

        report = self.manager.run(plan, self.session, causal={
            "signal": {"text": text, "plan_source": plan.source,
                       "state_version": plan.state_version,
                       "edge_cloud_level": route.level},
            "inference": {"actions": [{"device_id": s.device_id, "command": s.command}
                                      for s in plan.sub_tasks],
                          "context_used": plan.context_used,
                          "scope": plan.scope},
            "gate": {"high_risk": "manager", "param_envelope": "execution",
                     "lease": "plan-level"},
        })

        # 输出层声明校验：Manager 保证"执行真实发生"，本层保证"回复没说错"。
        # 只读、零 LLM、可配置关闭（trust.json → claim_check）。
        claim_cfg = load_claim_settings()
        claim = check_claims(report.reply, report.actions, report.results,
                             self._device_meta(), cfg=claim_cfg)
        reply = apply_suffix(report.reply, claim, claim_cfg) if claim.issues else report.reply

        # RAG：记录用户操作 / 场景习惯
        for r in report.results:
            if r.get("ok"):
                kb.record_command(r.get("device_id", ""), r.get("command", ""), r.get("params", {}))
        scene = self.root._match_scene(text)
        if scene:
            kb.record_scene(scene)

        self._remember(text, reply)

        trace = [t.to_dict() for t in report.trace]
        # 注入了哪些记忆：单独一条 trace，让"经验复用"在界面上是可复核的事实
        if plan.context_used:
            trace.insert(0, {"step": "recall",
                             "detail": self._describe_context(plan.context_used), "ok": True})
        # 工具/设备清单被裁了多少：可复核的"模型本轮看见了什么"
        if (plan.scope or {}).get("narrowed"):
            trace.insert(0, {"step": "scope", "detail": describe_scope(plan.scope), "ok": True})
        if claim.issues:
            trace.append({"step": "claim", "detail": "声明校验：" + claim.detail(), "ok": False})
        self._record_experience(plan, report, text)
        self._record_metrics(plan, trace, plan_ms, t_start)
        return {
            "reply": reply,
            "actions": report.actions,
            "results": report.results,
            "verification": report.verification,
            "claim_issues": [i.to_dict() for i in claim.issues],
            "scope": plan.scope or {},
            "draft": None,
            "draft_id": "",
            "decision_id": getattr(report, "decision_id", ""),
            "digital_human": {"emotion": "happy" if report.actions else "neutral", "action": "speak"},
            "decision_trace": trace,
            "edge_cloud": route.to_dict(),
            "requires_confirmation": report.requires_confirmation,
            "pending_id": report.pending_id,
            "pending_action": report.pending_action,
            "llm_budget": self._budget_view(trace),
        }

    # ---------- 闭环的另一半：把执行结果写回经验库 ----------
    def _record_experience(self, plan, report, text: str) -> None:
        """只记录**走慢环**的规划。

        规则命中的路径是确定的（同样的输入永远命中同样的规则），把它也写进经验库
        只会让库被"开灯/关灯"这类案例淹没，且它下回还会走规则、根本用不上经验。
        经验复用的价值恰恰在规则覆盖不到的那部分。
        """
        if plan.source != "llm":
            return
        total = len(report.results)
        if total == 0:
            return
        ok_count = sum(1 for r in report.results if r.get("ok"))
        rating = derive_rating(ok_count, total)
        experience.record(
            category="fuzzy_command",
            context_key=self._context_key(report.actions or plan.sub_tasks),
            request=text,
            actions=report.actions or [{"device_id": s.device_id, "command": s.command}
                                       for s in plan.sub_tasks],
            reply=report.reply,
            rating=rating,
            rating_source="auto_execution",
            source=plan.source,
        )

    def _context_key(self, actions) -> str:
        """把动作归成一个粗粒度上下文键，如「客厅|ac」。检索时按关键词重合度匹配。"""
        rooms, cats = [], []
        for a in actions or []:
            did = a.get("device_id", "") if isinstance(a, dict) else ""
            d = self.registry.get(did)
            if d is None:
                continue
            if d.room and d.room not in rooms:
                rooms.append(d.room)
            if d.category and d.category not in cats:
                cats.append(d.category)
        return "|".join(rooms + cats)

    @staticmethod
    def _describe_context(context_used: list) -> str:
        parts = []
        for c in context_used:
            if c.get("kind") == "manual":
                names = "、".join(n for n in c.get("names", []) if n)
                if names:
                    parts.append(f"手册：{names}")
            elif c.get("kind") == "experience":
                parts.append(f"经验#{c.get('id')}（{c.get('context_key', '')} "
                             f"{c.get('rating')}/5）")
        return "召回 " + "；".join(parts) if parts else ""

    # ---------- 观测：分层延迟 ----------
    def _record_metrics(self, plan, trace: list, plan_ms: float, t_start: float) -> None:
        """同时记两个延迟量：漏斗纯解析（parse_ms）与 decide 端到端（total_ms）。

        对外报"规则层延迟"一律用 total_ms —— 用户感受到的是它，
        `docs/BENCH.md` 的 8.46ms 也是它。把 parse_ms 当成规则层延迟会得到
        0.06ms 这种"好看但没意义"的数字（实测踩过：接口报 0.03ms、文档报 8.46ms，
        同名不同义）。
        """
        total_ms = (time.perf_counter() - t_start) * 1000.0
        snap = self.root.budget.snapshot()
        llm_log.record_decision(
            source=plan.source, parse_ms=plan_ms, total_ms=total_ms,
            actions=len(plan.sub_tasks or []),
            degraded=bool(snap.get("degraded")), degrade_reason=snap.get("reason", ""))

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

    def _device_meta(self) -> dict:
        """设备元数据（id → 名称/类别/房间），供输出层声明校验做指称匹配。

        只在 agent（装配层）里取设备信息，claim_checker 本身不 import 设备层 ——
        与 `main._device_name` 是同一个思路：只读层需要的事实由外面注入。
        """
        return {d.device_id: {"name": d.name, "category": d.category, "room": d.room}
                for d in self.registry.all()}

    def _remember(self, text: str, reply: str):
        self.session.append({"role": "user", "content": text})
        self.session.append({"role": "assistant", "content": reply})
        if len(self.session) > 20:
            self.session = self.session[-20:]
