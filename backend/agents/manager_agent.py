# -*- coding: utf-8 -*-
"""Manager Agent（执行护栏）。

**本类不调用大模型。** 职责是把 Root 已给出的具体动作逐条校验
（设备存在 / 命令合法）、按风险等级决定是否高危挂起、通过后执行并写入单一真相源。

为什么不做 LLM 翻译：原实现让 Manager 把 Root 的高层目标再翻译一次，每个子任务
各调一次模型，一次 decide() 的 LLM 调用数达 1+N。实测（experiments/README.md）
表明让 Root 直接输出具体动作的**幻觉率为 0%**，且即使出错也会被本类的 `_verify`
拒绝而非执行 —— 二次翻译既非必要，又引入了一条会畸形的 LLM 路径。

这正是「LLM 的错误由架构兜住，而不是靠 prompt 祈祷」的落点：
Root 只提议，Manager 决定提议能不能落地。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .. import execution
from ..devices.registry import DeviceRegistry
from ..lease import Lease, LeaseValidator
from ..state import StateStore, TraceStep


@dataclass
class ExecutionReport:
    reply: str
    actions: list = field(default_factory=list)      # 已执行动作
    results: list = field(default_factory=list)      # 执行结果
    trace: list = field(default_factory=list)        # 决策链路
    requires_confirmation: bool = False              # 是否高危待确认
    pending_id: str = ""
    pending_action: Optional[dict] = None            # 待确认动作描述
    # 本轮执行结局的汇总（confirmed / delivered / failed / rejected 各几条，
    # 以及哪些动作「已下发但未核实」）。存在的意义：证据链要能一句话报出来，
    # 而不是让调用方自己遍历 results 猜。
    verification: dict = field(default_factory=dict)
    #: 六段因果链的索引（/api/trust/{decision_id} 用它查完整链条）
    decision_id: str = ""


class ManagerAgent:
    def __init__(self, registry: DeviceRegistry, store: StateStore, capabilities=None):
        self.registry = registry
        self.store = store
        self.capabilities = capabilities

    def run(self, task_plan, history: Optional[list] = None,
            causal: Optional[dict] = None) -> ExecutionReport:
        """history 参数保留仅为签名兼容，本类不使用（不调 LLM、无会话依赖）。

        `causal` 是六段链的前三段（signal / inference / gate），由调用方提供；
        缺省时链上只会有 lease/action/readback，不会伪造前段。
        """
        decision_id = execution.new_decision_id()
        self.store.open_decision(decision_id, getattr(task_plan, "source", "rule"), causal)
        trace = [TraceStep("ground", f"状态版本 v{task_plan.state_version}，{len(task_plan.sub_tasks)} 个子任务")]
        executed, results = [], []

        # 零 LLM：Root 已给出具体动作，这里只做形状规整
        plans = self._plan_actions(task_plan.sub_tasks, trace)

        for sub_task, actions in plans:
            for action in actions:
                # 签发租约（绑定 Root 批准的 plan 版本）
                lease = self._issue_lease(sub_task, action, task_plan)

                # 校验动作合法性（幻觉拦截：设备不存在 / 命令非法 → 丢弃）
                ok, reason = self._verify(action)
                if not ok:
                    trace.append(TraceStep("verify", reason, ok=False))
                    self.store.append_stage(decision_id, "gate",
                                            {"rejected": reason, "action": action})
                    self.store.set_failed_layer(decision_id, "gate")
                    results.append({"ok": False, "status": "rejected", "error": reason,
                                    "device_id": action.get("device_id"),
                                    "command": action.get("command"),
                                    "failed_layer": "gate"})
                    continue

                # 高危操作 → 挂起待二次确认
                device = self.registry.get(action["device_id"])
                if device and LeaseValidator.is_high_risk(device.category, action["command"]):
                    desc = LeaseValidator.high_risk_desc(device.category, action["command"])
                    trace.append(TraceStep("verify", f"高危操作挂起待确认：{desc}（{device.name}）"))
                    pending_id = self.store.add_pending(lease, action, task_plan.reply)
                    pending_action = {
                        "device_id": action["device_id"],
                        "device_name": device.name,
                        "command": action["command"],
                        "desc": desc,
                    }
                    self.store.append_stage(decision_id, "gate", {
                        "suspended": "high_risk", "desc": desc,
                        "pending_id": pending_id,
                    })
                    self.store.set_failed_layer(decision_id, "gate")
                    return ExecutionReport(
                        reply=f"⚠️ 「{desc}」属于高危操作，请在弹窗中确认后执行。",
                        actions=executed, results=results, trace=trace,
                        requires_confirmation=True, pending_id=pending_id,
                        pending_action=pending_action,
                        verification=self._summarize(results),
                        decision_id=decision_id,
                    )

                # 执行 + 记录（审计/回读/撤销记录都在 execution 里统一写）
                result = self._execute(action, lease, decision_id=decision_id)
                executed.append(action)
                results.append(result)
                status = result.get("status") or ("ok" if result.get("ok") else "failed")
                detail = f"{action['device_id']}.{action['command']} → {status}"
                if result.get("ok") and result.get("status") == "delivered":
                    # 「已下发但未核实」必须出现在决策链路里，不能只在审计里。
                    rb_reason = (result.get("readback") or {}).get("reason") or "回读未核实"
                    detail += f"（未核实：{rb_reason}）"
                trace.append(TraceStep("execute", detail, ok=result.get("ok", False)))

        return ExecutionReport(reply=task_plan.reply, actions=executed, results=results,
                               trace=trace, requires_confirmation=False,
                               verification=self._summarize(results),
                               decision_id=decision_id)

    # ---------- 计划规整（零 LLM） ----------
    def _plan_actions(self, sub_tasks, trace) -> list[tuple]:
        """把 TaskPlan 的子任务规整为 [(sub_task, actions), ...]，全程不调 LLM。

        主路径：子任务已带具体动作（来自规则/技能/能力契约，或 Root 的模型提议），直接采纳。
        兜底：仅有高层 `goal` 的子任务，尝试能力契约确定性查表（仍是 0ms）。
        查表也不中的，产出一条 ok=False 的 trace 说明，交由上层兜底回复，
        而不是再去调一次模型 —— 那样会重新引入一条会畸形的 LLM 路径。
        """
        plans = []
        for sub_task in sub_tasks:
            if sub_task.device_id and sub_task.command:
                origin = "规则命中" if sub_task.source == "rule" else "模型提议"
                trace.append(TraceStep(
                    "propose", f"{origin}：{sub_task.device_id}.{sub_task.command}"))
                plans.append((sub_task, [{
                    "device_id": sub_task.device_id,
                    "command": sub_task.command,
                    "params": sub_task.params,
                }]))
                continue

            local = self._resolve_by_capability(sub_task.goal, trace)
            if local is None:
                trace.append(TraceStep("propose", f"无法解析目标：{sub_task.goal}", ok=False))
            plans.append((sub_task, local or []))
        return plans

    def _resolve_by_capability(self, goal: str, trace) -> Optional[list[dict]]:
        """能力契约确定性查表（0ms）。命中返回动作列表，未命中返回 None。"""
        if self.capabilities is None or not goal:
            return None
        cap = self.capabilities.resolve(goal)
        if not cap or not cap.get("command"):
            return None
        devices = self.capabilities.find_devices(cap["capability"], self.registry)
        if not devices:
            return None
        target = devices[0]
        trace.append(TraceStep("propose", f"能力映射：{cap['capability']} → {target.name}"))
        return [{"device_id": target.device_id, "command": cap["command"],
                 "params": dict(cap["params"])}]

    # ---------- 授权 ----------
    def _issue_lease(self, sub_task, action: dict, task_plan) -> Lease:
        device = self.registry.get(action.get("device_id", ""))
        category = device.category if device else sub_task.category
        return Lease.issue(
            action.get("device_id", ""), action.get("command", ""), action.get("params", {}),
            source=sub_task.source, state_version=task_plan.state_version, category=category,
        )

    # ---------- 校验 ----------
    def _verify(self, action: dict) -> tuple[bool, str]:
        device = self.registry.get(action.get("device_id", ""))
        if device is None:
            return False, f"设备不存在：{action.get('device_id')}"
        command = action.get("command", "")
        if command not in device.commands:
            return False, f"命令「{command}」不受设备「{device.name}」支持"
        ok, reason = self._verify_params(device, command, action.get("params") or {})
        if not ok:
            return False, reason
        return True, "ok"

    @staticmethod
    def _verify_params(device, command: str, params: dict) -> tuple[bool, str]:
        """参数包络校验（实现已收口到 `execution.verify_params`，此处仅保留入口）。"""
        return execution.verify_params(device, command, params)

    # ---------- 执行 ----------
    def _execute(self, action: dict, lease: Lease, decision_id: str = "") -> dict:
        """交给唯一写入口 `execution.execute_action`：租约过期/包络/审计/撤销都在那里。"""
        return execution.execute_action(
            action["device_id"], action["command"], action.get("params", {}),
            registry=self.registry, store=self.store,
            source=lease.source if lease else "rule", lease=lease,
            decision_id=decision_id, allow_high_risk=False)

    # ---------- 确认后执行高危动作 ----------
    def execute_pending(self, pending_id: str) -> dict:
        item = self.store.pop_pending(pending_id)
        if not item:
            return {"ok": False, "error": "待确认操作不存在或已过期"}
        lease = item["lease"]
        action = item["action"]
        if LeaseValidator.is_expired(lease):
            return {"ok": False, "error": "租约已过期，请重新发起指令"}
        # 确认后仍要过参数包络：挂起期间设备属性/配置可能已改（例如型号切换后
        # 取值范围变了），不能因为"用户点过确认"就跳过校验。
        device = self.registry.get(action.get("device_id", ""))
        if device is not None:
            ok, reason = self._verify_params(device, action.get("command", ""),
                                             action.get("params") or {})
            if not ok:
                return {"ok": False, "error": reason,
                        "device_id": action.get("device_id"),
                        "command": action.get("command")}
        decision_id = execution.new_decision_id()
        self.store.open_decision(decision_id, "confirm", {
            "signal": {"pending_id": pending_id},
            "gate": {"confirmed_by": "user", "high_risk": True},
        })
        return execution.execute_action(
            action["device_id"], action["command"], action.get("params", {}),
            registry=self.registry, store=self.store, source="confirm",
            lease=lease, decision_id=decision_id, allow_high_risk=True)

    # ---------- 证据汇总 ----------
    @staticmethod
    def _summarize(results: list) -> dict:
        """把本轮 results 汇成一句话能报的结局（供返回体与 trace 使用）。"""
        counts = {"confirmed": 0, "delivered": 0, "failed": 0, "error": 0, "rejected": 0}
        unverified = []
        for r in results or []:
            if not isinstance(r, dict):
                continue
            status = r.get("status") or ("rejected" if not r.get("ok") else "confirmed")
            counts[status] = counts.get(status, 0) + 1
            if status == "delivered":
                unverified.append({
                    "device_id": r.get("device_id", ""),
                    "reason": (r.get("readback") or {}).get("reason", ""),
                })
        total = sum(counts.values())
        return {
            "total": total,
            **counts,
            "all_confirmed": bool(total) and counts["confirmed"] == total,
            "unverified": unverified,
        }
