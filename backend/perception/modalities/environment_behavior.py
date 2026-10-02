# -*- coding: utf-8 -*-
"""环境行为模态：从传感器读数与操作日志推算居家状态。

这是感知层里**唯一完全不依赖新增硬件/依赖**的一路，数据全部来自灵犀已有的
传感器仿真与操作日志，因此它的置信度与真值来源都最清楚：
- 舒适度 ← 温湿度/空气质量与舒适区间的偏离
- 活跃度 ← 近窗口内设备操作次数（人在家的行为留痕）
- 作息规律度 ← 与用户历史习惯的偏离

**映射公式是工程选择，不是实测标定值。** 例如"舒适度 0.6、作息 0.4"这个配比
没有实验依据，只是让结果落在合理区间。它们全部外置在
`config/situation_mapping.yaml`，便于现场调参与后续标定。任何引用本模态数值的
文档都必须同时说明这一点。
"""
from __future__ import annotations

from typing import Optional

from ..fusion import ModalReading
from .base import ModalityDetector, PerceptionContext

# 舒适区间（可被 situation_mapping.yaml 覆盖）
COMFORT_BAND = {
    "temp_indoor": (20.0, 26.0),
    "humidity": (40.0, 60.0),
    "air_quality": (60.0, 100.0),
}
#: 活跃度饱和参考：近窗口内操作次数达到该值即认为"非常活跃"
ACTIVITY_SATURATION = 6

# 效价合成权重（工程选择，非标定值）
W_COMPORT = 0.6
W_ROUTINE = 0.4


class EnvironmentBehaviorModality(ModalityDetector):
    """传感器 + 操作日志 → 情境读数。"""

    name = "environment_behavior"
    description = "居家环境与行为模式（温度/湿度/空气质量/活动强度/作息）"
    local_only = True

    def __init__(self, comfort_band: Optional[dict] = None,
                 activity_saturation: int = ACTIVITY_SATURATION):
        self.comfort_band = comfort_band or COMFORT_BAND
        self.activity_saturation = activity_saturation

    def available(self) -> bool:
        return True

    # ---------- 子指标 ----------
    def _comfort_score(self, sensors: dict) -> tuple[float, dict]:
        """舒适度 [0,1]：各传感器落在舒适区间内的比例（含区间外的线性衰减）。"""
        if not sensors:
            return 0.0, {}
        detail, scores = {}, []
        for key, (lo, hi) in self.comfort_band.items():
            if key not in sensors:
                continue
            value = float(sensors[key])
            span = max(hi - lo, 1e-6)
            if lo <= value <= hi:
                score = 1.0
            else:
                # 偏离越远越低，偏离一个区间宽度即归零
                score = max(0.0, 1.0 - abs(value - (lo if value < lo else hi)) / span)
            scores.append(score)
            detail[key] = {"value": value, "band": [lo, hi], "score": round(score, 3)}
        return (sum(scores) / len(scores) if scores else 0.0), detail

    def _activity_score(self, recent_actions: list) -> float:
        return min(1.0, len(recent_actions) / max(1, self.activity_saturation))

    def _routine_score(self, habit: dict) -> tuple[float, dict]:
        """作息规律度 [0,1]。缺少习惯数据时返回中性 0.5 并说明。"""
        if not habit:
            return 0.5, {"source": "无习惯数据，取中性值 0.5"}
        deviation = habit.get("routine_deviation")
        if deviation is None:
            return 0.5, {"source": "习惯数据无 routine_deviation 字段，取中性值 0.5"}
        dev = max(0.0, float(deviation))
        return max(0.0, 1.0 - dev / 3.0), {"routine_deviation": round(dev, 3)}

    # ---------- 主入口 ----------
    def detect(self, ctx: PerceptionContext) -> Optional[ModalReading]:
        comfort, comfort_detail = self._comfort_score(ctx.sensors or {})
        activity = self._activity_score(ctx.recent_actions or [])
        routine, routine_detail = self._routine_score(ctx.habit or {})

        has_data = bool(ctx.sensors) or bool(ctx.recent_actions)
        if not has_data:
            # 没有任何环境数据时不编造读数 —— 与"零信息不制造中间值"同一原则
            return None

        # 效价：舒适与规律各占一部分权重，减 0.5 使中性态落在 0 附近
        valence = W_COMPORT * comfort + W_ROUTINE * routine - 0.5
        arousal = activity

        # 置信度取"数据充分度"启发式：传感器覆盖与操作样本各半。
        # 这是启发式而非标定值，调参入口在 situation_mapping.yaml。
        sensor_score = min(1.0, len(ctx.sensors or {}) / 4)
        action_score = min(1.0, len(ctx.recent_actions or []) / 5)
        confidence = 0.3 + 0.7 * (sensor_score + action_score) / 2

        return ModalReading(
            modality=self.name,
            valence=max(-1.0, min(1.0, valence)),
            arousal=max(0.0, min(1.0, arousal)),
            confidence=max(0.0, min(1.0, confidence)),
            label=self._label(valence, arousal),
            local_only=True,
            detail={
                "comfort": round(comfort, 3),
                "activity": round(activity, 3),
                "routine": round(routine, 3),
                "sensors": comfort_detail,
                "habit": routine_detail,
                "sample_size": {
                    "sensors": len(ctx.sensors or {}),
                    "recent_actions": len(ctx.recent_actions or []),
                },
                "note": "映射为工程选择，非实测标定值",
            },
        )

    @staticmethod
    def _label(valence: float, arousal: float) -> str:
        if valence < -0.15 and arousal < 0.3:
            return "居家状态偏低"
        if arousal >= 0.6:
            return "居家活跃"
        if valence > 0.15:
            return "居家舒适"
        return "居家平稳"
