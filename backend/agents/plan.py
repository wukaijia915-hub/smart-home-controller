# -*- coding: utf-8 -*-
"""任务计划数据结构（中立模块）。

放在这里而不是 root_agent.py 里，是为了让工具层能构造 TaskPlan 交给 Manager 执行，
同时避免 `toolkit → agents.root_agent → toolkit.schema_gen` 的循环导入。

这样做的意义：**工具调用与对话调用共用同一条护栏链**。此前 `/api/tools/call`
与 MCP `tools/call` 直接调 registry.control，完全绕过租约与高危确认 ——
实测可直接解锁门锁、点燃燃气灶。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubTask:
    """一个子任务。

    source="rule" 时 device_id/command/params 已由规则或技能填好；
    source="llm"  时同样带具体动作（Root 直接提议，见 experiments/README.md 实验二）；
    source="tool" 来自工具层直调（/api/tools/call、MCP）；
    仅有 goal 而无具体动作时，由 Manager 尝试能力契约查表兜底。
    """
    source: str = "rule"                 # rule | llm | tool
    device_id: str = ""
    command: str = ""
    params: dict = field(default_factory=dict)
    category: str = ""                   # 设备类别（风险判定用）
    goal: str = ""                       # 仅有高层意图时的描述


@dataclass
class TaskPlan:
    is_command: bool
    reply: str
    sub_tasks: list = field(default_factory=list)
    state_version: int = 0
    source: str = "rule"
    # 本次规划实际注入了哪些记忆（经验案例 id / 手册名）。空表示没用上任何记忆。
    # 存在的意义是**让"经验复用"可被复核**：答辩时能指出这一轮到底参考了哪几条
    # 历史案例，而不是只能声称"系统会学习"。
    context_used: list = field(default_factory=list)                 # rule | llm | tool | fallback
