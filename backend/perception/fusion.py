# -*- coding: utf-8 -*-
"""多模态置信度融合（移植自心镜 `backend/tools/multi_modal_fusion.py`，Apache-2.0）。

## 移植了什么，改了什么

心镜的原算法是**二模态**（面部 + 前庭）、固定 V-A 二维空间的融合，三步：

    1. 置信度
       - 面部：熵归一化   c = 1 - H / log(N)          （H 为情绪概率分布的香农熵）
       - 前庭：窗口方差   c = 1 - mean(σ) / MAX_STD    （时间序列越稳越可信）
    2. 自适应加权          w_i = c_i / Σc
       V_fused = Σ w_i · V_i ，fused_conf = mean(c_i)
    3. 模态一致性复核      两模态 V-A 的欧氏距离 d > 0.5 → 判为冲突

本文件保留这三步与心镜的两个常数（`MODAL_DISTANCE_THRESHOLD=0.5`、
`MAX_STD_FOR_CONFIDENCE=15.0`）以及它的 7 类情绪 V/A 参考表，改动只有两处：

- **二模态 → N 模态**：一致性复核从"两点距离"改为"最大两两距离"，
  两个模态时与心镜完全等价
- **参数化**：置信度由各模态自己算（每个模态最清楚自己该怎么判断可信度），
  fusion 只提供 `entropy_confidence()` / `stability_confidence()` 两个标准实现

## 边界（重要）

融合结果**只用于生成询问式建议，绝不进入控制回路**，见 `perception/__init__.py`
的 `PERCEPTION_CANNOT_ACTUATE`。理由是心镜自己的评测数据：其前庭引擎 14 个参数
修复后仅 2/14 ANOVA 显著、稳定性 ICC 多为负值，微运动特征对效价 r=0.021（≈无信号）。
未经充分验证的测量手段不应当作控制回路的输入。

这不是"不用它"，而是"接入它、用一致性检验管住它、如实报告边界"。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ---- 心镜原表：7 类情绪标签与 V/A 参考点（用于面部模态与标签化） ----
EMOTION_7_CLASSES = ["开心", "中性", "悲伤", "愤怒", "惊讶", "害怕", "厌恶"]

EMOTION_7_VA = {
    "开心": (0.80, 0.60),
    "中性": (0.00, 0.00),
    "悲伤": (-0.70, -0.20),
    "愤怒": (-0.60, 0.80),
    "惊讶": (0.20, 0.90),
    "害怕": (-0.40, 0.70),
    "厌恶": (-0.60, 0.40),
}

# 心镜原文："(根据常模 SD 的 50% 设定)"
MAX_STD_FOR_CONFIDENCE = 15.0
# 心镜原文：模态一致性复核阈值，两模态 V-A 欧氏距离超过此值需复核
MODAL_DISTANCE_THRESHOLD = 0.5
# 融合置信度下限：低于此值即使模态一致也不采信（"一致地都不确定"同样没有信息量）
MIN_FUSED_CONFIDENCE = 0.2


# ---------------------------------------------------------------- 置信度
def entropy_confidence(probs: dict) -> float:
    """熵置信度：`c = 1 - H / log(N)`（心镜 `compute_facial_confidence_entropy`）。

    概率分布越集中越可信。分布均匀时 H = log(N)，c = 0；完全确定时 c = 1。
    """
    if not probs:
        return 0.0
    n_classes = len(probs)
    if n_classes <= 1:
        return 1.0 if probs else 0.0
    values = [p for p in probs.values() if p and p > 0]
    if not values:
        return 0.0
    entropy = -sum(p * math.log(p) for p in values)
    max_entropy = math.log(n_classes)
    if max_entropy <= 0:
        return 0.0
    # 心镜原实现未 clamp；这里补上，避免浮点误差把置信度顶到略大于 1
    return max(0.0, min(1.0, 1.0 - entropy / max_entropy))


def stability_confidence(window_results: list, param_keys: list,
                         max_std: float = MAX_STD_FOR_CONFIDENCE) -> float:
    """稳定性置信度：`c = 1 - mean(σ) / max_std`（心镜 `compute_vestibular_confidence_variance`）。

    时间序列跨窗口标准差越小 → 信号越稳定 → 越可信。
    """
    if not window_results:
        return 0.0
    if len(window_results) < 2:
        # 心镜原文：窗口数不足时给 0.5 的默认置信度
        return 0.5

    stds = []
    for key in param_keys:
        values = [w.get(key, 0.0) for w in window_results]
        mean_val = sum(values) / len(values)
        variance = sum((v - mean_val) ** 2 for v in values) / len(values)
        stds.append(math.sqrt(variance))
    if not stds:
        return 0.0

    mean_std = sum(stds) / len(stds)
    return max(0.0, min(1.0, 1.0 - mean_std / max_std))


def classify_emotion_7class(valence: float, arousal: float) -> str:
    """V/A → 最近邻的 7 类标签（心镜 `classify_emotion_7class`）。"""
    best_label, best_dist = "中性", float("inf")
    for label, (v_ref, a_ref) in EMOTION_7_VA.items():
        dist = math.hypot(valence - v_ref, arousal - a_ref)
        if dist < best_dist:
            best_label, best_dist = label, dist
    return best_label


# ---------------------------------------------------------------- 数据结构
@dataclass
class ModalReading:
    """一个模态的一次读数。

    模态只负责报告"我看到了什么、我有多确定、我的原始依据是什么"，
    可信度判断由自身完成（`confidence`），融合器不越俎代庖。
    """
    modality: str                       # vision_behavior / facial_expression / ...
    valence: float                      # [-1, 1]
    arousal: float                      # [-1, 1]
    confidence: float                   # [0, 1]
    detail: dict = field(default_factory=dict)   # 原始依据（可解释性的载体）
    local_only: bool = True             # 原始数据是否端侧处理（画面/音频不出户）
    label: str = ""                     # 模态自报的语义标签（可选）

    def to_dict(self) -> dict:
        return {
            "modality": self.modality,
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "confidence": round(self.confidence, 3),
            "label": self.label,
            "local_only": self.local_only,
            "detail": self.detail,
        }


@dataclass
class FusionOutcome:
    """融合结果。"""
    valence: float
    arousal: float
    confidence: float
    label: str                          # 7 类标签之一
    weights: dict = field(default_factory=dict)      # 模态 → 权重
    modal_distance: float = 0.0                      # 最大两两距离（心镜为两点距离）
    consistent: bool = True                          # False = 冲突，结果应被丢弃
    reason: str = ""
    contributions: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "confidence": round(self.confidence, 3),
            "label": self.label,
            "weights": {k: round(v, 3) for k, v in self.weights.items()},
            "modal_distance": round(self.modal_distance, 3),
            "consistent": self.consistent,
            "reason": self.reason,
            "contributions": self.contributions,
        }


# ---------------------------------------------------------------- 融合器
class MultiModalFuser:
    """N 模态置信度融合器（心镜二模态融合的推广）。

    两个模态时与心镜 `MultiModalEmotionFuser.fuse()` 等价。
    """

    def __init__(self, distance_threshold: float = MODAL_DISTANCE_THRESHOLD):
        self.distance_threshold = distance_threshold

    def fuse(self, readings: list) -> Optional[FusionOutcome]:
        """执行融合。无有效读数时返回 None。"""
        readings = [r for r in readings if r is not None]
        if not readings:
            return None

        confidences = {r.modality: max(0.0, min(1.0, float(r.confidence))) for r in readings}

        # ⚠️ 这里有意识地**偏离**心镜原实现。
        # 心镜原文："两者置信度都极低，返回中间值" —— 把置信度都设为 0.5。
        # 那等于从"零信息"里凭空造出一个中等置信度的结果，接着就会照常参与决策。
        # 本项目不做这件事：零信息就应该表现为零置信度，由 MultiModalGate 拒绝。
        # 这与我们不做"未经充分验证就当控制回路输入"的原则是同一条。
        if sum(confidences.values()) < 0.001:
            return FusionOutcome(
                valence=0.0, arousal=0.0, confidence=0.0, label="",
                weights={k: 0.0 for k in confidences},
                modal_distance=0.0, consistent=False,
                reason="所有模态置信度均接近 0，无可用依据（不制造中间值）",
                contributions=[r.to_dict() for r in readings],
            )

        total = sum(confidences.values())
        weights = {k: v / total for k, v in confidences.items()}

        valence = sum(weights[r.modality] * r.valence for r in readings)
        arousal = sum(weights[r.modality] * r.arousal for r in readings)
        fused_conf = sum(confidences.values()) / len(confidences)   # 心镜：取平均

        # ---- 模态一致性复核：最大两两欧氏距离（二模态时退化为心镜的两点距离）----
        max_dist = 0.0
        worst_pair = None
        for i in range(len(readings)):
            for j in range(i + 1, len(readings)):
                a, b = readings[i], readings[j]
                d = math.hypot(a.valence - b.valence, a.arousal - b.arousal)
                if d > max_dist:
                    max_dist, worst_pair = d, (a.modality, b.modality)

        consistent = max_dist <= self.distance_threshold
        if consistent:
            reason = f"模态一致（最大距离 {max_dist:.2f} ≤ {self.distance_threshold}）"
        else:
            pair = f"{worst_pair[0]} ↔ {worst_pair[1]}" if worst_pair else "未知模态对"
            reason = (f"模态冲突：{pair} 判定距离 {max_dist:.2f} > "
                      f"{self.distance_threshold}，结果不可采信")

        return FusionOutcome(
            valence=valence,
            arousal=arousal,
            confidence=fused_conf,
            label=classify_emotion_7class(valence, arousal),
            weights=weights,
            modal_distance=max_dist,
            consistent=consistent,
            reason=reason,
            contributions=[r.to_dict() for r in readings],
        )
