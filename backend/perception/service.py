# -*- coding: utf-8 -*-
"""感知层对外入口：跑模态 → 融合 → 门控 → 生成询问式建议。

## 输出契约

`PerceptionService.perceive(ctx)` 返回 `PerceptionResult`，其中 `suggestion`
**永远是一个问句或 None，绝不是一条指令**。数字人拿到它只能播报，
不能据此调用任何设备接口。

## 与事件路由的分工（避免两套告警）

跌倒、安全隐患这类**紧急事件**已经由 `event_routes.json` 声明式路由处理
（触发设备联动 + 数字人告警）。感知层**不重复处理**它们 —— 否则同一个跌倒会
产生两条告警。感知层只负责"氛围/状态"这一类没有明确触发条件、需要综合判断的场景。

这条分界在 `_build_suggestion()` 里显式实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .fusion import MultiModalFuser
from .modal_gate import MultiModalGate
from .modalities import ModalityDetector, PerceptionContext, default_modalities

#: 这些事件类型已由 event_routes 处理，感知层不再生成建议，避免重复告警
ROUTED_ELSEWHERE = {"fall_detected", "safety_hazard"}


@dataclass
class PerceptionResult:
    readings: list = field(default_factory=list)          # 各模态读数
    gate: dict = field(default_factory=dict)              # 门控结果（含 evidence）
    suggestion: Optional[str] = None                      # 询问式建议（或 None）
    suggestion_basis: str = ""                            # 建议依据（可解释性）
    modality_status: list = field(default_factory=list)   # 各模态可用性与元信息
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "readings": [r.to_dict() for r in self.readings],
            "gate": self.gate,
            "suggestion": self.suggestion,
            "suggestion_basis": self.suggestion_basis,
            "modality_status": self.modality_status,
            "note": self.note,
        }


class PerceptionService:
    """感知层统一入口。"""

    def __init__(self, modalities: Optional[list] = None,
                 gate: Optional[MultiModalGate] = None):
        self.modalities: list = modalities if modalities is not None else default_modalities()
        self.gate = gate or MultiModalGate()

    # ---------- 主入口 ----------
    def perceive(self, ctx: PerceptionContext) -> PerceptionResult:
        status = [m.status() for m in self.modalities if isinstance(m, ModalityDetector)]

        readings = []
        for modality in self.modalities:
            try:
                reading = modality.detect(ctx)
            except Exception:
                # 一个模态坏掉不应该让整个感知层挂掉
                continue
            if reading is not None:
                readings.append(reading)

        gate_result = self.gate.evaluate(readings)
        suggestion, basis = self._build_suggestion(readings, gate_result)

        return PerceptionResult(
            readings=readings,
            gate=gate_result.to_dict(),
            suggestion=suggestion,
            suggestion_basis=basis,
            modality_status=status,
            note=("未通过门控，结果不可采信" if not gate_result.passed else ""),
        )

    # ---------- 建议生成（只出问句） ----------
    @staticmethod
    def _build_suggestion(readings: list, gate_result) -> tuple[Optional[str], str]:
        # 紧急事件交由事件路由处理，感知层不重复
        for r in readings:
            if r.detail.get("event_type") in ROUTED_ELSEWHERE:
                return None, "该事件由事件路由处理，感知层不重复告警"

        if not gate_result.passed:
            return None, f"未通过门控：{gate_result.reason}"

        if not readings:
            return None, "无模态读数"

        valence = gate_result.evidence.get("valence", 0.0)
        arousal = gate_result.evidence.get("arousal", 0.0)
        label = gate_result.evidence.get("label", "")

        if valence < -0.15 and arousal < 0.4:
            return ("看起来家里现在比较安静，要不要把灯光调暖一点、放点轻音乐？",
                    f"融合判定「{label}」效价 {valence:.2f} 唤醒 {arousal:.2f}")
        if valence < -0.15 and arousal >= 0.4:
            return ("感觉这会儿家里氛围有点紧绷，需要我帮忙调整下环境吗？",
                    f"融合判定「{label}」效价 {valence:.2f} 唤醒 {arousal:.2f}")
        if arousal >= 0.6:
            return ("家里挺热闹的，需要把环境调得更舒服些吗？",
                    f"融合判定「{label}」唤醒 {arousal:.2f}")
        return None, f"状态平稳（{label}），不必打扰"

    # ---------- 展示 ----------
    def status(self) -> dict:
        return {
            "modalities": [m.status() for m in self.modalities],
            "distance_threshold": self.gate.fuser.distance_threshold,
            "min_fused_confidence": self.gate.min_confidence,
        }
