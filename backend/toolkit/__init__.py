# -*- coding: utf-8 -*-
"""工具层（L4）：LLM 影响物理世界的唯一出口。

模块职责
- schema_gen.py  从设备属性元数据反推工具契约（schema 永不漂移）
- registry.py    工具注册表 + 参数校验 + 执行（阶段 1 接入 ActuationGuard）
- views.py       ★阶段 1：root_tools() 只读视图 / manager_tools() 可写视图
- meta_tools.py  ★阶段 1：面向 LLM 的元工具实现
- guard.py       ★阶段 1：ActuationGuard —— 工具层与护栏层的唯一接缝
"""
from .schema_gen import Tool, ToolParameter, tool_for, tool_name, build_tools
from .registry import ToolRegistry, tool_registry

__all__ = [
    "Tool", "ToolParameter", "tool_for", "tool_name", "build_tools",
    "ToolRegistry", "tool_registry",
]
