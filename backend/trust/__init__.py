# -*- coding: utf-8 -*-
"""信任层：决策解释（explain）与撤销（undo）。

边界：本目录**不执行设备动作**（`tests_trust.py` 用 AST 扫描 `registry.control(`）。
真正的执行由 `backend/execution.py` 负责；撤销只生成逆动作。
"""
from .explain import attribute_failure, explain, explain_recent
from .undo import (UndoRecord, UndoStore, apply_undo, can_undo, inverse_actions,
                   snapshot_attributes, undo_store)

__all__ = [
    "attribute_failure", "explain", "explain_recent",
    "UndoRecord", "UndoStore", "undo_store",
    "snapshot_attributes", "inverse_actions", "can_undo", "apply_undo",
]
