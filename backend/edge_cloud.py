# -*- coding: utf-8 -*-
"""端云协同调度层（命题核心模块，对应命题背景 #2 与答题要求 #2）。

命题要解决的矛盾是：家庭场景包含极度敏感的隐私数据（音视频流、作息规律），
而端侧设备算力有限，难以本地跑大模型；数据全量上云又有隐私泄露 + 延迟风险。

本模块的解法是「三级数据分级 + 端云任务路由 + 端侧小模型 + 隐私脱敏 + 上云审计」：

- L0 高敏（音视频流 / 人脸 / 作息 / 健康 / 跌倒）→ **强制端侧本地处理，绝不上云**；
- L1 中敏（环境传感器 / 设备状态 / 能耗）→ 可上云，但**仅上传脱敏摘要**；
- L2 低敏（意图语义 / 指令文本）→ 可直接上云做复杂推理。

端侧小模型 = 本地轻量意图引擎（意图模板 + 槽位抽取），<100ms、数据不出家庭；
生产环境可替换为本地 1B~7B 量化小模型 / NPU 推理，对外行为完全一致。
云侧大模型 = 远端 OpenAI 兼容 LLM，只处理复杂跨设备规划、模糊语义。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 一、数据分级（家庭数据敏感度三级模型）
# ---------------------------------------------------------------------------
class PrivacyLevel:
    L0 = "L0"   # 高敏：音视频流 / 人脸 / 生物特征 / 作息 / 健康 —— 绝不出门
    L1 = "L1"   # 中敏：环境传感器读数 / 设备状态 / 能耗 —— 脱敏后可上云
    L2 = "L2"   # 低敏：意图语义 / 指令文本 / 控制命令 —— 可直接上云


# 高敏触发词（L0）：命中即强制本地处理，绝不上云
L0_KEYWORDS = [
    "摄像头", "录像", "视频", "监控", "拍照", "画面", "人脸", "照片", "图像", "截图",
    "语音", "录音", "麦克风", "说话", "声音",
    "健康", "血压", "心率", "血氧", "血糖", "体温", "睡眠", "作息",
    "吃药", "服药", "用药", "病历", "就诊", "身体", "体检", "诊断",
    "跌倒", "摔倒", "晕倒", "老人", "小孩", "婴儿", "独居", "看护",
]

# 中敏触发词（L1）：环境/设备状态类，可脱敏上云
L1_KEYWORDS = [
    "温度", "湿度", "光照", "空气质量", "能耗", "用电", "电量", "电费",
    "设备状态", "传感器", "门锁状态",
]

# 高敏传感器键（传感器数据本身属 L0，如摄像头流、健康体征）
L0_SENSOR_KEYS = ["camera", "video", "audio", "heart_rate", "blood_pressure", "health"]


class PrivacyGuard:
    """隐私守护：数据分级 + 脱敏 + 上云数据审计（数据最小化原则）。"""

    def __init__(self):
        self.upload_audit: list[dict] = []   # 上云数据审计（append-only）

    # ---------- 分级 ----------
    def classify(self, text: str, sensors: Optional[dict] = None,
                 modalities: Optional[list] = None) -> str:
        """对输入做敏感度分级，返回 PrivacyLevel.L0 / L1 / L2。"""
        text = text or ""
        sensors = sensors or {}
        modalities = modalities or []

        # 1) 多模态通道：音视频通道天然高敏
        if any(m in ("voice", "vision", "camera", "audio", "video") for m in modalities):
            return PrivacyLevel.L0
        # 2) 高敏传感器键
        if any(k in sensors for k in L0_SENSOR_KEYS):
            return PrivacyLevel.L0
        # 3) 高敏关键词
        for kw in L0_KEYWORDS:
            if kw in text:
                return PrivacyLevel.L0
        # 4) 中敏关键词
        for kw in L1_KEYWORDS:
            if kw in text:
                return PrivacyLevel.L1
        # 5) 其余意图文本 → 低敏
        return PrivacyLevel.L2

    # ---------- 脱敏 ----------
    def sanitize(self, text: str) -> str:
        """上云前脱敏：遮蔽手机号、具体住址等可识别信息。"""
        masked = text or ""
        masked = re.sub(r"1[3-9]\d{9}", "1**********", masked)          # 手机号
        masked = re.sub(r"[一-龥]{2,4}(省|市|区|县|路|街|号|栋|单元|室)", "***", masked)  # 地址
        return masked

    # ---------- 上云审计 ----------
    def record_upload(self, level: str, raw: str, sanitized: str) -> dict:
        """记录一次上云数据（用于审计「数据最小化」是否被执行）。"""
        entry = {
            "ts": time.time(),
            "privacy_level": level,
            "raw_length": len(raw or ""),
            "sanitized": sanitized,
            "minimized": (level == PrivacyLevel.L1),   # 中敏数据仅上传脱敏摘要
        }
        self.upload_audit.append(entry)
        if len(self.upload_audit) > 200:
            self.upload_audit = self.upload_audit[-200:]
        return entry

    def upload_count(self) -> int:
        return len(self.upload_audit)

    def audit_snapshot(self, limit: int = 20) -> list[dict]:
        return self.upload_audit[-limit:]


# ---------------------------------------------------------------------------
# 二、端云路由决策
# ---------------------------------------------------------------------------
@dataclass
class RouteDecision:
    level: str = PrivacyLevel.L2              # 数据敏感度分级
    cloud_allowed: bool = True                # 是否允许上云（L0 恒为 False）
    need_sanitize: bool = False               # 上云前是否需要脱敏（L1 为 True）
    target: str = "edge_first"                # edge_only | edge_first
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "cloud_allowed": self.cloud_allowed,
            "need_sanitize": self.need_sanitize,
            "target": self.target,
            "reason": self.reason,
        }


class DecisionRouter:
    """端云任务路由：根据隐私等级决定任务在端侧还是云端处理。

    路由策略（端侧小模型优先，云端大模型兜底）：
    - L0 高敏 → edge_only（强制本地，绝不调用云端 LLM）；
    - L1 中敏 → edge_first（本地规则优先，规则未命中可上云，但需脱敏）；
    - L2 低敏 → edge_first（本地规则优先，规则未命中直接上云）。
    """

    def __init__(self, guard: Optional[PrivacyGuard] = None):
        self.guard = guard or PrivacyGuard()

    def route(self, text: str, sensors: Optional[dict] = None,
              modalities: Optional[list] = None) -> RouteDecision:
        level = self.guard.classify(text, sensors, modalities)

        if level == PrivacyLevel.L0:
            return RouteDecision(
                level=level, cloud_allowed=False, need_sanitize=False,
                target="edge_only",
                reason="高敏数据（音视频流/人脸/作息/健康），强制端侧本地处理，绝不上云",
            )
        if level == PrivacyLevel.L1:
            return RouteDecision(
                level=level, cloud_allowed=True, need_sanitize=True,
                target="edge_first",
                reason="中敏数据（环境/设备状态），端侧优先，规则未命中时仅上传脱敏摘要到云端",
            )
        return RouteDecision(
            level=level, cloud_allowed=True, need_sanitize=False,
            target="edge_first",
            reason="低敏意图文本，端侧优先，规则未命中时上云做复杂推理",
        )
