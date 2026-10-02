# -*- coding: utf-8 -*-
"""ActuationGuard —— 所有设备动作的唯一执行出口。

## 存在的理由

工具层（`POST /api/tools/call`、MCP `tools/call`）此前直接调用
`DeviceRegistry.control()`，**完全绕过了授权租约、状态版本校验与高危二次确认**。
实测可直接执行：

    lock_entry_01__unlock        → 门锁被解锁
    gas_stove_kitchen_01__turn_on → 燃气灶被点燃

这与项目宣称的「授权租约是防越权/重放的核心护栏」直接矛盾。

## 为什么不做第二套校验

容易想到的实现是"给工具层也写一遍租约校验"。**不要那样做** —— 两套校验必然迟早
分叉（改了一处忘另一处），而分叉的护栏比没有护栏更危险：它让人以为已经防住了。

这里改为把工具调用包装成单动作 `TaskPlan`，交给同一个 `ManagerAgent` 执行。
于是**对话路径与工具路径共用同一条链**：

    参数校验 → 租约签发 → 版本校验 → 高危挂起待确认 → 回读确认 → 写审计

只此一条，无双份语义。
"""
from __future__ import annotations

from typing import Optional

from ..agents.plan import SubTask, TaskPlan


class ManagerGuard:
    """把工具调用适配为 TaskPlan 并交给 ManagerAgent 执行。"""

    def __init__(self, manager):
        self.manager = manager

    def actuate(self, device_id: str, command: str, params: Optional[dict] = None,
                tool: Optional[str] = None) -> dict:
        """执行一个设备动作，全程受 Manager 的护栏约束。

        返回：
        - 执行成功：DeviceRegistry.control() 的结果（含 status/readback）
        - 高危挂起：{"ok": False, "requires_confirmation": True, "pending_id": ...}
        - 校验失败：{"ok": False, "error": ...}
        """
        if not device_id or not command:
            return {"ok": False, "error": "device_id 与 command 不能为空"}

        device = self.manager.registry.get(device_id)
        plan = TaskPlan(
            is_command=True,
            reply=f"已执行 {tool or f'{device_id}.{command}'}",
            sub_tasks=[SubTask(
                source="tool",
                device_id=device_id,
                command=command,
                params=dict(params or {}),
                category=device.category if device else "",
            )],
            state_version=self.manager.store.version,
            source="tool",
        )
        report = self.manager.run(plan)

        if report.requires_confirmation:
            return {
                "ok": False,
                "requires_confirmation": True,
                "pending_id": report.pending_id,
                "pending_action": report.pending_action,
                "error": report.reply,
                "note": "高危操作已挂起，请调用 POST /api/confirm 并传入 pending_id 确认",
            }
        if not report.results:
            return {"ok": False, "error": report.reply or "动作未被执行"}
        return report.results[0]
