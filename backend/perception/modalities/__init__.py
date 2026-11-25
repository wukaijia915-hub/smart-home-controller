# -*- coding: utf-8 -*-
"""模态检测器集合。

每个模态独立、可单测、互不知道对方存在；融合与采信判断分别在
`perception/fusion.py` 与 `perception/modal_gate.py`。

默认组合（`default_modalities()`）刻意包含**可能不可用**的模态：
面部表情需要可选依赖、前庭需要外部引擎数据。它们不可用时返回 None，
融合置信度随之下降 —— 这是正确行为，不是缺陷。
"""
from __future__ import annotations

from .base import ModalityDetector, PerceptionContext
from .environment_behavior import EnvironmentBehaviorModality
from .facial_expression import DEEPFACE_AVAILABLE, FacialExpressionModality
from .vestibular import VestibularModality
from .vision_behavior import VisionBehaviorModality

__all__ = [
    "ModalityDetector",
    "PerceptionContext",
    "VisionBehaviorModality",
    "FacialExpressionModality",
    "EnvironmentBehaviorModality",
    "VestibularModality",
    "DEEPFACE_AVAILABLE",
    "default_modalities",
]


def default_modalities() -> list:
    """感知层的默认模态组合（顺序即证据链展示顺序）。"""
    return [
        VisionBehaviorModality(),
        EnvironmentBehaviorModality(),
        FacialExpressionModality(),      # 无 DeepFace 时自动降级为不可用
        VestibularModality(),            # 无外部引擎数据时返回 None
    ]
