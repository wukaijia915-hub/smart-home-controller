# -*- coding: utf-8 -*-
"""Manager Agent（域管理 + 执行护栏）。

职责：把 Root 下发的子任务翻译成具体设备动作（LLM 路径二次推理），
逐动作校验（设备存在 / 命令合法 / 参数范围），高危操作挂起待确认，
通过校验后执行设备并写入单一真相源（StateStore）。
是「只提议、不执行」的 Root 与设备之间的最后一道校验闸门。
"""
from __future__ import annotations

import json
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
    def __init__(self, registry: DeviceRegistry, store: StateStore, llm):
        self.registry = registry
        self.store = store
        self.llm = llm

    def run(self, task_plan, history: Optional[list] = None) -> ExecutionReport:
        trace = [TraceStep("ground", f"状态版本 v{task_plan.state_version}，{len(task_plan.sub_tasks)} 个子任务")]
        executed, results = [], []

        for sub_task in task_plan.sub_tasks:
            actions = self._translate(sub_task, history, trace)
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

    # ---------- 翻译 ----------
    def _translate(self, sub_task, history, trace) -> list[dict]:
        if sub_task.source == "rule":
            trace.append(TraceStep("propose", f"规则命中：{sub_task.device_id}.{sub_task.command}"))
            return [{"device_id": sub_task.device_id, "command": sub_task.command, "params": sub_task.params}]
        # LLM 路径：把高层 goal 翻译成具体动作（第二次 LLM 调用）
        trace.append(TraceStep("propose", f"LLM 翻译目标：{sub_task.goal}"))
        return self._llm_translate(sub_task.goal, history)

    def _llm_translate(self, goal: str, history: Optional[list]) -> list[dict]:
        system = self._build_system_prompt()
        user = f"操作目标：{goal}\n请翻译为具体设备动作。"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for m in (history or [])[-6:]:
            messages.insert(-1, m)
        result = self.llm.chat_json(messages, max_tokens=300, context_text=goal)
        if not result:
            return []
        return [a for a in result.get("actions", [])
                if isinstance(a, dict) and a.get("device_id") and a.get("command")]

    def _build_system_prompt(self) -> str:
        device_desc = []
        for d in self.registry.all():
            attrs = {k: a.value for k, a in d.attributes.items()}
            device_desc.append(
                f"{d.device_id}({d.name},{d.room},{d.category},属性={json.dumps(attrs, ensure_ascii=False)},命令={d.commands})"
            )
        return (
            "你是智能家居中控的执行者 Manager，负责把高层操作目标翻译成具体设备动作。\n"
            "输出 JSON，格式：\n"
            '{"actions": [{"device_id": "light_living_01", "command": "set_brightness", "params": {"brightness": 20}}]}\n'
            "规则：\n"
            "1. device_id 必须从设备列表中选择，command 必须是该设备声明的命令之一。\n"
            "2. 一个目标可能需要多个动作（如\"开空调调到24度\"=turn_on+set_temperature），按需拆分。\n"
            "3. 若目标无法映射到任何设备，返回空 actions: []。\n"
            f"设备列表(id,名称,房间,类别,属性,命令):\n" + "\n".join(device_desc) + "\n"
            "只输出 JSON，不要输出其他文字。"
        )

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
