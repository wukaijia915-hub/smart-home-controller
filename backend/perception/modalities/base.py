# -*- coding: utf-8 -*-
"""模态检测器的公共基类与感知上下文。

一个模态的职责边界（刻意收窄）：
    输入一段上下文 → 输出**一个** `ModalReading`（我在 V/A 上看到什么、有多确定、依据是什么）
    或者 `None`（本模态本次无法提供读数）

模态**不做**的事：
    - 不互相调用、不知道其它模态存在（融合由 `fusion.py` 统一做）
    - 不产生设备动作（见 `perception/__init__.py` 的 PERCEPTION_CANNOT_ACTUATE）
    - 不判断"该不该采信自己" —— 那是 `MultiModalGate` 的职责；模态只如实报告置信度

这三条让每个模态都能独立单测，也让"某个模态坏了"的故障不会横向扩散。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from ..fusion import ModalReading


@dataclass
class PerceptionContext:
    """一次感知所需的全部输入。

    纯软件阶段这些字段都由仿真/日志填充；接入真实硬件时只需换数据来源，
    模态检测器本身不用改。
    """
    #: 端侧视觉检测的场景提示（软件阶段用于模拟视觉事件）
    scene_hint: str = ""
    #: 当前传感器读数（温度/湿度/光照/空气质量/人体红外）
    sensors: dict = field(default_factory=dict)
    #: 近期设备操作记录，用于推算活动强度
    recent_actions: list = field(default_factory=list)
    #: 用户习惯统计（来自 memory/habits）
    habit: dict = field(default_factory=dict)
    #: 摄像头帧（可选）。仅在安装了可选视觉依赖时可用；原始帧不出本机
    face_frame: Any = None
    #: 前庭振动读数（可选，由外部引擎算好后传入）。
    #: 形状：{"valence": float, "arousal": float, "window_results": [每窗口参数], "K_value": float}
    vestibular: dict = field(default_factory=dict)

    # ---- 以下为白盒情境参数（situation.py）所需的时序输入 ----
    #: 连续在位累计分钟数（由 PresenceTracker 维护）
    presence_minutes: float = 0.0
    #: 距上次活动的分钟数
    idle_minutes: float = 0.0
    #: 疑久卧时长（无摄像头证据时为 0，不参与判定）
    lying_minutes: float = 0.0
    #: 评估时刻（用于深夜活动判定；默认取当前时间）
    now: Any = None

    def to_dict(self) -> dict:
        """用于审计/落盘的脱敏视图 —— 不包含任何原始画面数据。"""
        return {
            "scene_hint": self.scene_hint,
            "sensors": self.sensors,
            "recent_action_count": len(self.recent_actions),
            "has_face_frame": self.face_frame is not None,
            "vestibular_windows": len(self.vestibular.get("window_results", []) or []),
            "presence_minutes": round(self.presence_minutes, 1),
            "idle_minutes": round(self.idle_minutes, 1),
        }


class ModalityDetector(ABC):
    """模态检测器基类。"""

    #: 模态标识（出现在融合权重与证据链里，需稳定）
    name: str = "modality"
    #: 人可读的说明
    description: str = ""
    #: 原始数据是否端侧处理（画面/音频类模态必须是 True）
    local_only: bool = True

    @abstractmethod
    def detect(self, ctx: PerceptionContext) -> Optional[ModalReading]:
        """给出一次读数。无法提供时返回 None，不要编造。"""
        raise NotImplementedError

    def available(self) -> bool:
        """本模态当前是否可用（依赖是否就绪）。默认可用。"""
        return True

    def status(self) -> dict:
        return {"name": self.name, "description": self.description,
                "local_only": self.local_only, "available": self.available()}
