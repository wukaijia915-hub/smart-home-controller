# -*- coding: utf-8 -*-
"""视觉行为模态：有人/无人/跌倒/安全隐患。

数据源复用既有的 `backend/multimodal.py::VisionEventDetector` —— 它在软件阶段用
关键词模拟端侧视觉小模型的输出，行为契约与真实端侧推理一致：
**画面本地处理，只输出结构化事件结论**。

视觉事件是离散的、有明确真值的检测结果（有人在/没人/摔倒了），不是连续测量。
这一点很重要：它不像情绪测量那样需要心理测量学验证，可以用混淆矩阵直接评估，
所以它是感知层里**最可靠**的一路证据。
"""
from __future__ import annotations

from typing import Optional

from ...multimodal import VisionEventDetector
from ..fusion import ModalReading
from .base import ModalityDetector, PerceptionContext

# 视觉事件 → (效价, 唤醒度) 的工程映射。
# 这是**设计选择**而非实测标定值：跌倒/隐患属紧急负性高唤醒，有人属温和正性，
# 无人属中性低唤醒。数值可由 situation_mapping.yaml 覆盖，便于现场调参演示。
EVENT_VA_MAP = {
    "fall_detected":  (-0.70, 0.90),
    "safety_hazard":  (-0.60, 0.80),
    "person_present": (0.20, 0.50),
    "no_person":      (0.00, 0.10),
}
EVENT_LABEL = {
    "fall_detected": "跌倒",
    "safety_hazard": "安全隐患",
    "person_present": "有人活动",
    "no_person": "无人",
}


class VisionBehaviorModality(ModalityDetector):
    """端侧视觉事件 → 情境读数。"""

    name = "vision_behavior"
    description = "端侧视觉事件检测（画面本地处理，只输出事件结论）"
    local_only = True

    def __init__(self, detector: Optional[VisionEventDetector] = None):
        self.detector = detector or VisionEventDetector()

    def available(self) -> bool:
        return True        # 软件阶段由规则模拟，无需额外依赖

    def detect(self, ctx: PerceptionContext) -> Optional[ModalReading]:
        event = self.detector.detect(ctx.scene_hint)
        if event is None:
            return None

        va = EVENT_VA_MAP.get(event.event_type)
        if va is None:
            return None

        return ModalReading(
            modality=self.name,
            valence=va[0],
            arousal=va[1],
            # 事件自带置信度（端侧检测器的输出），直接采信它在语义上是对的：
            # 视觉事件的"置信度"就是检测器对这个离散结论的确信程度
            confidence=float(event.confidence),
            label=EVENT_LABEL.get(event.event_type, event.event_type),
            local_only=True,
            detail={
                "event_type": event.event_type,
                "room": event.room,
                "privacy_level": event.privacy_level,
                "local_processed": event.local_processed,
                "source": "edge_vision",
            },
        )
