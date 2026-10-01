# -*- coding: utf-8 -*-
"""兼容垫片 —— 工具层已迁移到 `backend.toolkit`。

保留本模块仅为兼容既有导入路径（main.py / mcp.py / tests_error.py）。
新代码请直接 `from .toolkit import tool_registry, Tool`。

原实现在此废弃的三个原因：
1. 12 处 `dev.execute(...)` 调用 Device 上不存在的方法，工具执行 100% 失败；
2. 参数名与 virtual_devices.apply_command 读取的键名系统性漂移（D13）；
3. 工具名按房间命名（set_客厅_light），同房间多台同类设备互相覆盖，
   且燃气灶类别完全没有注册分支。
详见 backend/toolkit/schema_gen.py 头部说明。
"""
from __future__ import annotations

from .toolkit.registry import ToolRegistry, tool_registry
from .toolkit.schema_gen import Tool, ToolParameter, build_tools, tool_for, tool_name

__all__ = [
    "Tool", "ToolParameter", "ToolRegistry", "tool_registry",
    "build_tools", "tool_for", "tool_name",
]
