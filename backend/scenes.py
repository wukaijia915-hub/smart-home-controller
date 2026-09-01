# -*- coding: utf-8 -*-
"""标准场景模式定义（对应验收指标中的标准场景集 S1-S10）。
优先从 config/scenes.json 加载，不存在则回退硬编码。
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "scenes.json"

# 硬编码兜底（JSON 不存在时使用）
_FALLBACK_SCENES = {
    "home": {
        "desc": "回家模式",
        "actions": [
            {"device_id": "light_living_01", "command": "set_brightness", "params": {"brightness": 80}},
            {"device_id": "curtain_living_01", "command": "open", "params": {}},
            {"device_id": "ac_living_01", "command": "turn_on", "params": {}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 26}},
        ],
    },
    "away": {
        "desc": "离家模式",
        "actions": [
            {"device_id": "light_living_01", "command": "turn_off", "params": {}},
            {"device_id": "light_living_02", "command": "turn_off", "params": {}},
            {"device_id": "light_bedroom_01", "command": "turn_off", "params": {}},
            {"device_id": "curtain_living_01", "command": "close", "params": {}},
            {"device_id": "curtain_bedroom_01", "command": "close", "params": {}},
            {"device_id": "ac_living_01", "command": "turn_off", "params": {}},
            {"device_id": "ac_bedroom_01", "command": "turn_off", "params": {}},
            {"device_id": "tv_living_01", "command": "turn_off", "params": {}},
        ],
    },
    "movie": {
        "desc": "观影模式",
        "actions": [
            {"device_id": "light_living_01", "command": "set_brightness", "params": {"brightness": 20}},
            {"device_id": "light_living_01", "command": "set_color_temp", "params": {"color_temp": 3000}},
            {"device_id": "light_living_02", "command": "set_brightness", "params": {"brightness": 30}},
            {"device_id": "curtain_living_01", "command": "close", "params": {}},
            {"device_id": "tv_living_01", "command": "turn_on", "params": {}},
        ],
    },
    "sleep": {
        "desc": "睡眠模式",
        "actions": [
            {"device_id": "light_living_01", "command": "turn_off", "params": {}},
            {"device_id": "light_living_02", "command": "turn_off", "params": {}},
            {"device_id": "tv_living_01", "command": "turn_off", "params": {}},
            {"device_id": "fan_living_01", "command": "turn_off", "params": {}},
            {"device_id": "humidifier_living_01", "command": "turn_off", "params": {}},
            {"device_id": "ac_living_01", "command": "turn_off", "params": {}},
            {"device_id": "curtain_living_01", "command": "close", "params": {}},
            {"device_id": "light_bedroom_01", "command": "set_brightness", "params": {"brightness": 10}},
            {"device_id": "ac_bedroom_01", "command": "turn_on", "params": {}},
            {"device_id": "ac_bedroom_01", "command": "set_temperature", "params": {"temperature": 26}},
            {"device_id": "ac_bedroom_01", "command": "set_mode", "params": {"mode": "auto"}},
            {"device_id": "curtain_bedroom_01", "command": "close", "params": {}},
        ],
    },
    "guest": {
        "desc": "会客模式",
        "actions": [
            {"device_id": "light_living_01", "command": "set_brightness", "params": {"brightness": 100}},
            {"device_id": "light_living_01", "command": "set_color_temp", "params": {"color_temp": 4500}},
            {"device_id": "light_living_02", "command": "set_brightness", "params": {"brightness": 60}},
            {"device_id": "curtain_living_01", "command": "open", "params": {}},
            {"device_id": "ac_living_01", "command": "turn_on", "params": {}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 24}},
        ],
    },
    "morning": {
        "desc": "起床模式",
        "actions": [
            {"device_id": "light_bedroom_01", "command": "set_brightness", "params": {"brightness": 60}},
            {"device_id": "curtain_bedroom_01", "command": "open", "params": {}},
            {"device_id": "curtain_living_01", "command": "open", "params": {}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 24}},
        ],
    },
    "cool": {
        "desc": "降温模式",
        "actions": [
            {"device_id": "ac_living_01", "command": "turn_on", "params": {}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 24}},
            {"device_id": "fan_living_01", "command": "turn_on", "params": {}},
        ],
    },
    "warm": {
        "desc": "升温模式",
        "actions": [
            {"device_id": "ac_living_01", "command": "turn_on", "params": {}},
            {"device_id": "ac_living_01", "command": "set_mode", "params": {"mode": "heat"}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 28}},
        ],
    },
    "save": {
        "desc": "节能模式",
        "actions": [
            {"device_id": "light_living_02", "command": "turn_off", "params": {}},
            {"device_id": "tv_living_01", "command": "turn_off", "params": {}},
            {"device_id": "fan_living_01", "command": "turn_off", "params": {}},
            {"device_id": "humidifier_living_01", "command": "turn_off", "params": {}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 28}},
            {"device_id": "ac_living_01", "command": "set_mode", "params": {"mode": "fan"}},
        ],
    },
    "night": {
        "desc": "晚安模式",
        "actions": [
            {"device_id": "light_living_01", "command": "turn_off", "params": {}},
            {"device_id": "light_living_02", "command": "turn_off", "params": {}},
            {"device_id": "light_bedroom_01", "command": "turn_off", "params": {}},
            {"device_id": "tv_living_01", "command": "turn_off", "params": {}},
            {"device_id": "curtain_living_01", "command": "close", "params": {}},
            {"device_id": "curtain_bedroom_01", "command": "close", "params": {}},
        ],
    },
}


def load_scenes() -> dict:
    """从 JSON 配置加载场景，失败则回退硬编码。"""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            scenes = data.get("scenes", {})
            if scenes:
                return scenes
        except Exception as e:
            print(f"[场景配置] JSON 加载失败，回退硬编码: {e}")
    return _FALLBACK_SCENES


SCENES = load_scenes()
