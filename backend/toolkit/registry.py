# -*- coding: utf-8 -*-
"""工具注册表：注册、发现、参数校验、执行、调用日志（可审计）。

执行路径的唯一性：所有工具调用都经 `_execute()`。
阶段 1 会通过 `bind_actuation_guard()` 注入 ActuationGuard，
届时工具调用将强制经过「参数包络 → 租约签发 → 版本校验 → 回读确认」，
使 LLM 无法绕过护栏直接影响设备。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .schema_gen import Tool, build_tools

logger = logging.getLogger("toolkit")


class ToolRegistry:
    """全局工具注册表。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._call_log: list[dict] = []
        self._registry = None                  # DeviceRegistry（执行目标）
        self._actuation_guard = None           # ★阶段 1 注入

    # ---------- 装配 ----------
    def build_from_devices(self, registry) -> None:
        """从设备注册表生成全部设备控制工具（幂等：重复调用不会重复注册）。"""
        self._registry = registry
        self._tools.clear()
        for tool in build_tools(registry.all()):
            self._tools[tool.name] = tool
        logger.info(f"工具注册表已构建：{len(self._tools)} 个工具 / {len(registry.all())} 台设备")

    def bind_actuation_guard(self, guard) -> None:
        """装配执行护栏。**不装配则设备类工具一律拒绝执行**（fail closed）。

        main.py 在启动时注入 `ManagerGuard(agent.manager)`，使工具路径与
        对话路径共用同一条护栏链。见 backend/toolkit/guard.py。
        """
        self._actuation_guard = guard

    # ---------- 注册与查询 ----------
    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        logger.debug(f"注册工具: {tool.name} ({tool.category})")

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self, category: Optional[str] = None) -> list[Tool]:
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        return tools

    def list_schemas(self) -> list[dict]:
        """输出所有工具的 OpenAI Function Calling schema。"""
        return [t.to_schema() for t in self._tools.values()]

    def get_call_log(self, limit: int = 50) -> list[dict]:
        return self._call_log[-limit:]

    # ---------- 参数校验 ----------
    @staticmethod
    def _validate(tool: Tool, params: dict) -> tuple[bool, str]:
        """按工具契约校验参数。返回 (是否通过, 原因)。"""
        for p in tool.parameters:
            if p.name not in params:
                if p.required:
                    return False, f"缺少必填参数: {p.name}"
                continue
            value = params[p.name]
            if p.enum and value not in p.enum:
                return False, f"参数 {p.name} 取值无效（{value!r}），可选值: {p.enum}"
            if p.type == "number":
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    return False, f"参数 {p.name} 必须是数值，实际收到 {value!r}"
                if p.min is not None and num < p.min:
                    return False, f"参数 {p.name}={value} 低于下限 {p.min}{p.unit}"
                if p.max is not None and num > p.max:
                    return False, f"参数 {p.name}={value} 超过上限 {p.max}{p.unit}"
        return True, "ok"

    # ---------- 执行 ----------
    def call(self, name: str, params: Optional[dict] = None) -> dict:
        """调用工具。返回 {ok, result, tool} 或 {ok: False, error}。"""
        params = params or {}
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"未知工具: {name}", "tool": name,
                    "supported": sorted(self._tools.keys())}

        ok, reason = self._validate(tool, params)
        if not ok:
            self._log(name, params, None, reason)
            return {"ok": False, "error": reason, "tool": name}

        try:
            result = self._execute(tool, params)
        except Exception as e:
            logger.error(f"工具调用失败 {name}: {e}")
            self._log(name, params, None, f"{type(e).__name__}: {e}")
            return {"ok": False, "error": str(e), "tool": name}

        success = bool(result.get("ok")) if isinstance(result, dict) else True
        self._log(name, params, result, None if success else str(result.get("error", "")))
        if not success and isinstance(result, dict):
            return {**result, "tool": name}
        return {"ok": True, "result": result, "tool": name}

    def _execute(self, tool: Tool, params: dict) -> dict:
        """统一执行入口 —— 所有工具调用都从这里出去。

        设备类工具**必须有护栏**。没有护栏时直接拒绝，而不是退回直连
        `registry.control()` —— 后者会静默绕过租约、版本校验与高危确认
        （实测可解锁门锁、点燃燃气灶）。宁可失败关闭，也不要悄悄留个洞。
        """
        if tool.handler is not None:
            return tool.handler(params)
        if self._actuation_guard is None:
            return {"ok": False,
                    "error": "工具执行护栏未装配，拒绝执行设备动作。"
                             "请先调用 bind_actuation_guard()（见 backend/toolkit/guard.py）"}
        return self._actuation_guard.actuate(
            tool.device_id, tool.command, params, tool=tool.name)

    def _log(self, name: str, params: dict, result, error: Optional[str]) -> None:
        self._call_log.append({
            "tool": name, "params": params, "result": result,
            "ok": error is None, "error": error,
        })
        if len(self._call_log) > 200:
            self._call_log.pop(0)

    # ---------- 外部工具（非设备类）注册辅助 ----------
    def register_external(self, name: str, description: str, handler: Callable,
                          category: str = "system", parameters: Optional[list] = None) -> Tool:
        """注册非设备类工具（场景/系统/知识/元工具）。供阶段 1 的 views.py 使用。"""
        tool = Tool(name=name, description=description, category=category,
                    parameters=parameters or [], device_id=None, command=None,
                    handler=handler)
        self.register(tool)
        return tool


# 全局单例
tool_registry = ToolRegistry()
