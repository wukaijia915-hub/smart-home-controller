# -*- coding: utf-8 -*-
"""兼容垫片：`knowledge.rag` 转发到 `backend/memory/` 的三个记忆源。

阶段 4 把原来堆在一个类里的「手册 + 习惯」拆成 `memory/{manuals,habits,experience}.py`
（三源各自单一职责，且 experience 是新增的第三源）。为避免一次性改动
`agent.py` / `root_agent.py` / `main.py` 的全部调用点，这里保留原来的
`kb` 门面与全部方法签名，内部只做转发。

**新代码不要再依赖本模块**，直接 `from ..memory import manuals, habits, experience`。
本文件只应在遗留调用点消失后删除。
"""
from __future__ import annotations

from ..memory import habits as _habits
from ..memory import manuals as _manuals


class _KnowledgeFacade:
    """旧 `KnowledgeBase` 接口，全部转发到 memory/。"""

    # ---------- 手册 ----------
    def search_manuals(self, query: str, top_k: int = 3) -> list[dict]:
        return _manuals.manuals.search(query, top_k=top_k)

    # ---------- 习惯 ----------
    def get_habit_hint(self, query: str = "") -> str:
        return _habits.habits.hint(query)

    def record_command(self, device_id: str, command: str, params: dict) -> None:
        _habits.habits.record_command(device_id, command, params)

    def record_scene(self, scene: str) -> None:
        _habits.habits.record_scene(scene)

    # ---------- 组合 ----------
    def get_context(self, query: str) -> str:
        """手册知识 + 用户习惯，拼成注入 prompt 的上下文块。"""
        parts = []
        manual_ctx = _manuals.manuals.get_context(query, top_k=2)
        if manual_ctx:
            parts.append(manual_ctx)
        hint = _habits.habits.hint(query)
        if hint:
            parts.append(f"【用户习惯参考】{hint}")
        return "\n".join(parts)

    def get_stats(self) -> dict:
        h = _habits.habits.stats()
        return {
            "manuals_count": len(_manuals.manuals.manuals),
            "total_commands": h["total_commands"],
            "total_scenes": h["total_scenes"],
            "top_scene": h["top_scene"],
            "declared_preferences": h["declared_preferences"],
            "conflicts": h["conflicts"],
            "habits_path": str(_habits.habits.path),
        }

    # ---------- 兼容旧属性访问 ----------
    @property
    def manuals(self) -> list[dict]:
        return _manuals.manuals.manuals

    @property
    def habits(self) -> dict:
        return _habits.habits.data


# 旧类名保留为别名：`knowledge/__init__.py` 与任何外部脚本仍可 `from .rag import KnowledgeBase`
KnowledgeBase = _KnowledgeFacade

kb = _KnowledgeFacade()
