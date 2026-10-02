# -*- coding: utf-8 -*-
"""面部表情模态（**演示级**）。

## 四条边界，写死在代码里

1. **演示级，非医学级。** 用 DeepFace 现成的 7 类表情分类器（FER 系训练集，
   公开基准约 65–70%）。本项目**不做任何准确率承诺**，也不把它当作心理测量工具。
2. **绝不进入控制回路。** 表情结果只参与"要不要主动问一句"的判断，
   不产生任何设备动作（由 `perception/__init__.py` 的硬边界与 `tests_perception.py`
   的静态检查共同保证）。
3. **端侧处理，只出标签。** 画面在本机完成推理，离开本机的只有 7 类概率分布，
   原始帧零字节外传。对应 `edge_cloud` 的 L0 最高敏分级。
4. **可选依赖，优雅降级。** DeepFace + TensorFlow 约 2GB，与"pip install 即跑"
   的工程取向冲突，因此放在 `requirements-vision.txt`。未安装时本模态
   `available()==False`、`detect()` 返回 None —— 感知层少一路证据但照常工作，
   融合的置信度会相应下降（而不是被编造出来）。

## 为什么还是接入它

因为它演示的是**机制**而不是准确率：多个独立模态各自给出带置信度的判断，
分歧时被一致性检验拦下。这个机制的有效性不依赖面部模型有多准 ——
事实上，心镜技术报告里最漂亮的一段正是"面部因光照误判为焦虑（mouth_curve=-0.93），
但前庭侧输出正效价，融合后纠正为开心"，即**单模态误判被多模态复核救回**。
"""
from __future__ import annotations

from typing import Optional

from ..fusion import EMOTION_7_VA, ModalReading, entropy_confidence
from .base import ModalityDetector, PerceptionContext

try:
    from deepface import DeepFace          # noqa: F401
    DEEPFACE_AVAILABLE = True
except Exception:                           # ImportError 或 TF 初始化失败
    DeepFace = None                         # type: ignore
    DEEPFACE_AVAILABLE = False

# DeepFace 输出的英文标签 → 本项目的 7 类中文标签
_LABEL_MAP = {
    "happy": "开心", "neutral": "中性", "sad": "悲伤", "angry": "愤怒",
    "surprise": "惊讶", "fear": "害怕", "disgust": "厌恶",
}


def probabilities_to_va(probs: dict) -> tuple[float, float]:
    """7 类概率分布 → (效价, 唤醒度)。

    按 `EMOTION_7_VA` 参考点做概率加权平均（心镜 `compute_va_from_probs` 的做法）。
    这是**工程映射**，不是心理测量学意义上的标定。
    """
    total = sum(p for p in probs.values() if p > 0)
    if total <= 0:
        return 0.0, 0.0
    v = sum(probs.get(k, 0.0) * va[0] for k, va in EMOTION_7_VA.items())
    a = sum(probs.get(k, 0.0) * va[1] for k, va in EMOTION_7_VA.items())
    return v / total, a / total


class FacialExpressionModality(ModalityDetector):
    """面部表情 → 情境读数（演示级）。"""

    name = "facial_expression"
    description = "面部表情识别（演示级，DeepFace FER，画面本地处理）"
    local_only = True

    def __init__(self, analyzer=None):
        self._analyzer = analyzer
        self._unavailable_reason = ""

    def available(self) -> bool:
        if self._analyzer is not None:
            return True
        if DEEPFACE_AVAILABLE:
            return True
        self._unavailable_reason = (
            "未安装 DeepFace（pip install -r requirements-vision.txt）；"
            "本模态跳过，融合置信度会相应下降")
        return False

    def detect(self, ctx: PerceptionContext) -> Optional[ModalReading]:
        if not self.available():
            return None
        if ctx.face_frame is None:
            return None

        try:
            raw = self._analyze(ctx.face_frame)
        except Exception:
            # 识别失败不抛异常：一个模态坏掉不应该让整个感知层挂掉
            return None

        probs_en = raw.get("emotion") or {}
        if not probs_en:
            return None
        probs = {_LABEL_MAP.get(k, k): v for k, v in probs_en.items()}

        valence, arousal = probabilities_to_va(probs)
        return ModalReading(
            modality=self.name,
            valence=valence,
            arousal=arousal,
            # 心镜原公式：熵置信度。分布越集中越可信
            confidence=entropy_confidence(probs),
            label=max(probs, key=probs.get) if probs else "",
            local_only=True,
            detail={
                "emotion_probs": {k: round(v, 3) for k, v in probs.items()},
                "frame_bytes_uploaded": 0,
                "privacy_level": "L0",
                "note": "演示级，非医学级；画面本地处理，仅概率分布参与融合",
            },
        )

    def _analyze(self, frame) -> dict:
        """调用 DeepFace。抽成方法便于测试时注入 fake analyzer。"""
        if self._analyzer is not None:
            return self._analyzer(frame)
        result = DeepFace.analyze(frame, actions=["emotion"], enforce_detection=False,
                                  silent=True)
        if isinstance(result, list):
            result = result[0]
        return result or {}
