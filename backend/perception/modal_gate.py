# -*- coding: utf-8 -*-
"""MultiModalGate —— 把多模态融合结果包成统一的门控结果。

与执行层的 `lease.LeaseGate` 共用 `backend/gate.py` 的 `Gate` 抽象：
两侧都是「默认不信任 → 交叉验证 → 不过就拒绝」，代码结构同构而非口头类比。

冲突（模态判定距离超阈值）时返回 `passed=False`，**调用方必须丢弃结果**，
不允许"降级使用"或"取平均凑一个值出来"。
"""
from __future__ import annotations

from typing import Optional

from ..gate import Gate, GateResult
from .fusion import MIN_FUSED_CONFIDENCE, MultiModalFuser


class MultiModalGate(Gate):
    """多模态一致性门控。"""

    name = "multi_modal"

    def __init__(self, fuser: Optional[MultiModalFuser] = None,
                 min_confidence: float = MIN_FUSED_CONFIDENCE):
        self.fuser = fuser or MultiModalFuser()
        self.min_confidence = min_confidence

    def evaluate(self, readings: list) -> GateResult:
        """评估一组模态读数。

        - 无有效读数     → passed=False（没有依据，不能凭空判断）
        - 模态判定冲突   → passed=False，evidence 里保留各模态原始读数供追查
        - 融合置信度过低 → passed=False（"一致地都不确定"同样没有信息量）
        - 通过           → passed=True，evidence 含权重与各模态贡献
        """
        readings = [r for r in readings if r is not None]
        if not readings:
            return GateResult(passed=False, confidence=0.0,
                              reason="无有效模态读数", evidence={"modalities": []})

        outcome = self.fuser.fuse(readings)
        if outcome is None:                                  # 理论上不会发生
            return GateResult(passed=False, confidence=0.0,
                              reason="融合失败", evidence={})

        evidence = outcome.to_dict()
        # 端侧处理标记：只要有一个模态的原始数据出了本机，这条就不为 True
        evidence["local_only"] = all(getattr(r, "local_only", True) for r in readings)

        if not outcome.consistent:
            return GateResult(passed=False, confidence=outcome.confidence,
                              reason=outcome.reason, evidence=evidence)

        if outcome.confidence < self.min_confidence:
            return GateResult(
                passed=False, confidence=outcome.confidence,
                reason=f"融合置信度 {outcome.confidence:.2f} 低于下限 "
                       f"{self.min_confidence}，一致但都不确定，不予采信",
                evidence=evidence)

        return GateResult(passed=True, confidence=outcome.confidence,
                          reason=outcome.reason, evidence=evidence)
