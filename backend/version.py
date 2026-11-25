# -*- coding: utf-8 -*-
"""版本信息（单一出处）。

改名沿革：本项目原名「灵犀 · 门控式大模型家居中枢」，2026-11 按攀登计划
申报书升级为「犀鉴」——定位从"门控式大模型中枢"转为
「基于 Wi-Fi CSI 的隐私优先家庭感知与可信自适应系统」，五层架构为
接入 / 房间 / 校验 / 信任 / 自治。

任何对外声明版本的地方都从 APP_VERSION 取，避免"文档写 2.0.0、镜像里是 1.1.0"。
"""
from __future__ import annotations

APP_VERSION = "2.0.0"
APP_NAME = "犀鉴 · 基于 Wi-Fi CSI 的隐私优先家庭感知与可信自适应系统"
VERSION_NOTES = (
    "v2.0.0：按 2027 广东攀登计划申报书重构 —— 五层架构（接入/房间/校验/信任/自治）、"
    "房间三态判定与六路旁证、统一执行入口与六段因果链、10 分钟撤销、"
    "L5 有界自适应（偏好带/目标仲裁/四道闸/冷启动/漂移降级）、设备 17→19。"
)
LEGACY_NAME = "灵犀"


def version_info() -> dict:
    return {"version": APP_VERSION, "name": APP_NAME, "notes": VERSION_NOTES,
            "legacy_name": LEGACY_NAME}
