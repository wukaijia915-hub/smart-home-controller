# -*- coding: utf-8 -*-
"""L5 自治层：偏好带 / 目标仲裁 / 四道闸 / 冷启动 / 漂移降级。

边界：本目录不执行设备动作（`tests_adaptation.py` 用 AST 扫描 `registry.control(`）。
执行由 `execution.execute_plan(source="adaptation")` 完成。
"""
from .objectives import Proposal, arbitrate
from .policy import AdaptationPolicy, AdaptationState
from .preference import PreferenceBand, best_band, learn_bands
from .service import AdaptationService

__all__ = [
    "Proposal", "arbitrate",
    "AdaptationPolicy", "AdaptationState",
    "PreferenceBand", "learn_bands", "best_band",
    "AdaptationService",
]
