# -*- coding: utf-8 -*-
"""前庭振动模态（适配器，默认不启用）。

## 关于这个模态的诚实说明（必读）

心镜最亮眼的卖点是 VibraImage 前庭振动情绪识别引擎（约 3000 行、基于 Minkin 专著
公式体系、配 10266 人常模）。**但它自己的评测数据不支持把它当作可靠的测量手段：**

| 指标 | 心镜 `vibraimage_engine资料/tests/` 实测 |
|---|---|
| ANOVA 显著参数 | 修复后 **2/14**（stress p=0.0186、self_regulation p=0.0002 但方向相反） |
| 稳定性 ICC | **多数为负值**（aggression −0.47、charm −0.39、suspicious −0.28） |
| 报告自陈局限 | 样本量最多 27 段视频；无原版 VibraImage 软件输出可对比；只能做内部一致性验证 |
| 用户自己的论文实验 | 微运动特征对效价 **r = 0.021（≈无信号）**；内容基线 r = 0.692 |

**ICC 为负意味着"同一被试重复测量结果不一致"** —— 这正是绝对情绪判定的致命伤。

## 所以本项目如何处理它

不是"不用"，也不是"照搬"，而是：

1. **只作为一路证据接入**，由 `MultiModalGate` 的一致性检验管住：
   若前庭判定与行为/视觉判定冲突，整组结果被丢弃，而不是把前庭的值算进去
2. **只出相对基线偏离，不出绝对情绪标签**。ICC 问题主要打击绝对值；
   纵向个人对比是它相对可行的用法
3. **绝不进入控制回路**
4. **本文件不包含引擎本体**。`detect()` 接收**外部算好的**读数
   （`PerceptionContext.vestibular`），只负责把它转成 `ModalReading` 并用心镜原公式
   `stability_confidence` 计算置信度。要真正启用它，需要另行把心镜的
   `backend/vibraimage/` 引擎接进来 —— 那是一个需要单独评估的决定，
   不是本项目顺手做的事

默认情况下 `PerceptionContext.vestibular` 为空，本模态返回 None，
感知层少一路证据但照常工作。
"""
from __future__ import annotations

from typing import Optional

from ..fusion import MAX_STD_FOR_CONFIDENCE, ModalReading, stability_confidence
from .base import ModalityDetector, PerceptionContext

# 心镜 E1-E12 参数名（用于跨窗口稳定性计算）
VIBRAIMAGE_PARAM_KEYS = [
    "aggression", "stress", "tension", "suspect",
    "balance", "charm", "energy", "self_regulation",
    "inhibition", "neuroticism", "depression", "happiness",
]


class VestibularModality(ModalityDetector):
    """前庭振动读数适配器（数据由外部引擎提供）。"""

    name = "vestibular"
    description = "前庭振动（适配器；引擎输出由外部提供，默认不启用）"
    local_only = True

    def __init__(self, max_std: float = MAX_STD_FOR_CONFIDENCE):
        self.max_std = max_std

    def available(self) -> bool:
        return True        # 适配器本身总是可用；是否有数据取决于上游是否接入引擎

    def detect(self, ctx: PerceptionContext) -> Optional[ModalReading]:
        payload = ctx.vestibular or {}
        if not payload:
            return None

        valence = payload.get("valence")
        arousal = payload.get("arousal")
        if valence is None or arousal is None:
            return None

        windows = payload.get("window_results") or []
        # 心镜原公式：窗口间标准差越小 → 信号越稳 → 越可信。
        # 无窗口序列时退化为低置信度，而不是给一个看着体面的默认值
        confidence = (stability_confidence(windows, VIBRAIMAGE_PARAM_KEYS, self.max_std)
                      if windows else 0.2)

        return ModalReading(
            modality=self.name,
            valence=float(valence),
            arousal=float(arousal),
            confidence=float(confidence),
            label="前庭读数",
            local_only=True,
            detail={
                "n_windows": len(windows),
                "k_value": payload.get("K_value"),
                "confidence_formula": "1 - mean(std)/max_std（心镜原公式）",
                "caveat": "ICC 多为负值，仅作一路证据；由一致性检验决定是否采信",
                "engine_present": bool(windows),
            },
        )
