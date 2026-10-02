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


class ManagerAgent:
    def __init__(self, registry: DeviceRegistry, store: StateStore, capabilities=None):
        self.registry = registry
        self.store = store
        self.capabilities = capabilities

    def run(self, task_plan, history: Optional[list] = None) -> ExecutionReport:
        """history 参数保留仅为签名兼容，本类不使用（不调 LLM、无会话依赖）。"""
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
                    results.append({"ok": False, "error": reason,
                                    "device_id": action.get("device_id"), "command": action.get("command")})
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
                    return ExecutionReport(
                        reply=f"⚠️ 「{desc}」属于高危操作，请在弹窗中确认后执行。",
                        actions=executed, results=results, trace=trace,
                        requires_confirmation=True, pending_id=pending_id, pending_action=pending_action,
                    )

                # 执行 + 记录
                result = self._execute(action, lease)
                executed.append(action)
                results.append(result)
                trace.append(TraceStep("execute",
                    f"{action['device_id']}.{action['command']} → {'成功' if result.get('ok') else '失败'}",
                    ok=result.get("ok", False)))

        return ExecutionReport(reply=task_plan.reply, actions=executed, results=results,
                               trace=trace, requires_confirmation=False)

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
        return True, "ok"

    # ---------- 执行 ----------
    def _execute(self, action: dict, lease: Lease) -> dict:
        if LeaseValidator.is_expired(lease):
            return {"ok": False, "error": "租约已过期", "device_id": action.get("device_id"),
                    "command": action.get("command")}
        result = self.registry.control(action["device_id"], action["command"], action.get("params", {}))
        self.store.bump()
        self.store.record(lease, action["device_id"], action["command"], action.get("params", {}), result)
        return result

    # ---------- 确认后执行高危动作 ----------
    def execute_pending(self, pending_id: str) -> dict:
        item = self.store.pop_pending(pending_id)
        if not item:
            return {"ok": False, "error": "待确认操作不存在或已过期"}
        lease = item["lease"]
        action = item["action"]
        if LeaseValidator.is_expired(lease):
            return {"ok": False, "error": "租约已过期，请重新发起指令"}
        result = self.registry.control(action["device_id"], action["command"], action.get("params", {}))
        self.store.bump()
        self.store.record(lease, action["device_id"], action["command"], action.get("params", {}), result)
        return result
