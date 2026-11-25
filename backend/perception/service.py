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
from .situation import SituationAssessor, SituationInput

#: 这些事件类型已由 event_routes 处理，感知层不再生成建议，避免重复告警
ROUTED_ELSEWHERE = {"fall_detected", "safety_hazard"}


@dataclass
class PerceptionResult:
    readings: list = field(default_factory=list)          # 各模态读数
    gate: dict = field(default_factory=dict)              # 门控结果（含 evidence）
    situation: dict = field(default_factory=dict)         # 白盒情境参数与归类
    suggestion: Optional[str] = None                      # 询问式建议（或 None）
    suggestion_basis: str = ""                            # 建议依据（可解释性）
    modality_status: list = field(default_factory=list)   # 各模态可用性与元信息
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "readings": [r.to_dict() for r in self.readings],
            "gate": self.gate,
            "situation": self.situation,
            "suggestion": self.suggestion,
            "suggestion_basis": self.suggestion_basis,
            "modality_status": self.modality_status,
            "note": self.note,
        }


class PerceptionService:
    """感知层统一入口。"""

    def __init__(self, modalities: Optional[list] = None,
                 gate: Optional[MultiModalGate] = None,
                 assessor: Optional[SituationAssessor] = None):
        self.modalities: list = modalities if modalities is not None else default_modalities()
        self.gate = gate or MultiModalGate()
        self.assessor = assessor or SituationAssessor()

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

        # 情境参数独立于门控计算：它是事实归纳（有真值来源），
        # 而门控管的是"多模态判定是否互相矛盾"。两者失败模式不同，不该互相牵连。
        situation = self.assessor.assess(SituationInput(
            sensors=ctx.sensors or {},
            recent_actions=ctx.recent_actions or [],
            presence_minutes=ctx.presence_minutes,
            idle_minutes=ctx.idle_minutes,
            lying_minutes=ctx.lying_minutes,
            history=ctx.habit or {},
            now=ctx.now,
        ))

        suggestion, basis = self._build_suggestion(ctx, readings, gate_result, situation)

        return PerceptionResult(
            readings=readings,
            gate=gate_result.to_dict(),
            situation=situation.to_dict(),
            suggestion=suggestion,
            suggestion_basis=basis,
            modality_status=status,
            note=("多模态未通过门控，氛围判断不可采信" if not gate_result.passed else ""),
        )

    # ---------- 建议生成（只出问句） ----------
    @staticmethod
    def _build_suggestion(ctx: PerceptionContext, readings: list, gate_result,
                          situation) -> tuple[Optional[str], str]:
        # 1) 紧急事件交由事件路由处理，感知层不重复
        for r in readings:
            if r.detail.get("event_type") in ROUTED_ELSEWHERE:
                return None, "该事件由事件路由处理，感知层不重复告警"

        region = situation.region
        region_label = situation.region_label

        # 2) 家中无人：说了也没人听。
        #    但该结论只有在房间态**确实**判为 empty 时才允许；degraded/uncertain
        #    一律不推断无人（申报书：不确定是刻意输出，不硬判）。
        if region == "away":
            if ctx.rooms and ctx.presence_state in ("degraded", "uncertain"):
                return None, (f"房间聚合状态为 {ctx.presence_state}，"
                              f"不据此推断无人（需旁证一致）")
            return None, f"{region_label}，无需播报"

        # 3) 情境异常优先于氛围判断 —— 它有可指认的真值来源，比融合出的氛围更硬
        if region == "anomaly":
            return ("注意到今天有些不太寻常的活动，需要我帮您留意一下吗？",
                    f"情境判定：{region_label}")
        if region == "prolonged_inactive":
            return ("有一阵子没检测到活动了，需要我帮您确认一下情况吗？",
                    f"情境判定：{region_label}")
        if region == "routine_shifted":
            return ("最近的作息好像和平时不太一样，要不要把灯光和温度按作息调一下？",
                    f"情境判定：{region_label}")
        if region == "discomfort":
            return ("现在室内环境不太舒服，要不要我帮您调整一下？",
                    f"情境判定：{region_label}")

        # 4) 氛围判断（依赖多模态融合，需通过一致性门控）
        if not gate_result.passed:
            return None, f"多模态未通过门控：{gate_result.reason}"

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
        return None, f"状态平稳（{region_label}），不必打扰"

    # ---------- 展示 ----------
    def status(self) -> dict:
        return {
            "modalities": [m.status() for m in self.modalities],
            "distance_threshold": self.gate.fuser.distance_threshold,
            "min_fused_confidence": self.gate.min_confidence,
            "situation_params": list((self.assessor.config.get("params") or {}).keys()),
        }
