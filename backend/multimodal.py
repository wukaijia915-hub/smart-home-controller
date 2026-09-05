# -*- coding: utf-8 -*-
"""多模态感知层（命题背景 #1 / 答题要求 #1）。

打破「固定唤醒词 + 标准指令」的交互范式，融合文本、语音、视觉、传感器多种输入模态，
并演示「端侧视觉小模型」如何实现主动智能（如老人跌倒检测 → 主动告警 + 设备联动）。

关键设计：视觉画面属 L0 高敏数据，检测在**端侧本地**完成，
只把「检测结论」（如 "跌倒"）作为低敏语义上云 / 触发告警，画面绝不出门。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .edge_cloud import PrivacyLevel


class Modality:
    TEXT = "text"
    VOICE = "voice"
    VISION = "vision"
    SENSOR = "sensor"


@dataclass
class VisionEvent:
    """端侧视觉检测事件（画面本地处理，仅输出结构化结论）。"""
    event_type: str                       # fall_detected | person_present | no_person | safety_hazard
    room: str = "客厅"
    confidence: float = 0.0
    description: str = ""
    local_processed: bool = True          # 恒为 True：画面本地处理，不传云
    privacy_level: str = PrivacyLevel.L0

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "room": self.room,
            "confidence": self.confidence,
            "description": self.description,
            "local_processed": self.local_processed,
            "privacy_level": self.privacy_level,
        }


class VisionEventDetector:
    """端侧视觉小模型（软件模拟）。

    生产环境为本地 NPU / 端侧 VLM（如移远端侧视觉大模型，50+ tokens/s）；
    本 MVP 用规则模拟「跌倒 / 占位」检测，行为一致：画面本地推理，只输出事件结论。
    """

    def __init__(self):
        # 可注入的检测规则：(事件类型, 触发信号) → 描述
        self._rules = {
            "fall_detected": "检测到有人跌倒（端侧视觉，画面本地处理，未上云）",
            "person_present": "检测到人员活动",
            "no_person": "未检测到人员",
            "safety_hazard": "检测到安全隐患（如明火/水渍）",
        }

    def detect(self, scene_hint: str = "") -> Optional[VisionEvent]:
        """根据场景提示返回一个检测事件（模拟端侧视觉推理）。"""
        hint = scene_hint or ""
        if any(k in hint for k in ("跌倒", "摔倒", "晕倒", "fall")):
            return VisionEvent("fall_detected", confidence=0.93,
                               description=self._rules["fall_detected"])
        if any(k in hint for k in ("离开", "无人", "出门", "no_person")):
            return VisionEvent("no_person", confidence=0.90,
                               description=self._rules["no_person"])
        if any(k in hint for k in ("明火", "燃气", "水渍", "隐患")):
            return VisionEvent("safety_hazard", confidence=0.88,
                               description=self._rules["safety_hazard"])
        if any(k in hint for k in ("有人", "活动", "走动")):
            return VisionEvent("person_present", confidence=0.95,
                               description=self._rules["person_present"])
        return None

    def event_to_alert(self, event: VisionEvent) -> Optional[dict]:
        """把视觉事件转化为主动告警（不含任何画面数据）。"""
        if event.event_type == "fall_detected":
            return {
                "level": "critical",
                "message": "⚠️ 端侧视觉检测到有人跌倒，已本地确认，是否联动开灯并通知紧急联系人？",
                "source": "edge_vision",
                "privacy": "画面本地处理，未上云",
            }
        if event.event_type == "safety_hazard":
            return {
                "level": "warning",
                "message": "⚠️ 端侧视觉检测到安全隐患，建议本地联动检查。",
                "source": "edge_vision",
                "privacy": "画面本地处理，未上云",
            }
        return None


class MultimodalContext:
    """多模态输入统一上下文：把文本 / 语音 / 视觉 / 传感器聚合为统一决策上下文。

    语音与视觉在端侧转为结构化语义后再参与决策，原始音视频流不出家庭。
    """

    def __init__(self):
        self.text: str = ""
        self.voice_transcript: str = ""       # 端侧 ASR 转写结果
        self.vision_events: list = []         # 端侧视觉事件
        self.sensors: dict = {}
        self.modalities: list = []

    def add_text(self, text: str) -> "MultimodalContext":
        self.text = text
        if Modality.TEXT not in self.modalities:
            self.modalities.append(Modality.TEXT)
        return self

    def add_voice(self, transcript: str) -> "MultimodalContext":
        self.voice_transcript = transcript
        if Modality.VOICE not in self.modalities:
            self.modalities.append(Modality.VOICE)
        return self

    def add_vision(self, event: VisionEvent) -> "MultimodalContext":
        self.vision_events.append(event)
        if Modality.VISION not in self.modalities:
            self.modalities.append(Modality.VISION)
        return self

    def add_sensors(self, sensors: dict) -> "MultimodalContext":
        self.sensors = sensors
        if Modality.SENSOR not in self.modalities:
            self.modalities.append(Modality.SENSOR)
        return self

    def to_dict(self) -> dict:
        return {
            "modalities": self.modalities,
            "text": self.text,
            "voice_transcript": self.voice_transcript,
            "vision_events": [e.to_dict() for e in self.vision_events],
            "sensors": self.sensors,
        }
