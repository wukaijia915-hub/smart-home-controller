# -*- coding: utf-8 -*-
"""环境与传感器仿真：模拟昼夜光照、温湿度波动、人体红外、空气质量。
传感器为只读数据源，与设备状态联动（开灯→光照上升、开空调→温度趋稳等）。
优先从 config/environment.json 加载参数，不存在则使用默认值。
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from pathlib import Path
from typing import Callable, Optional

from .devices.registry import DeviceRegistry

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "environment.json"

_DEFAULT_CONFIG = {
    "sensors": {
        "temp_indoor": {"initial": 25, "min": 16, "max": 35, "fluctuation": 0.3},
        "humidity": {"initial": 50, "min": 20, "max": 90, "fluctuation": 1.0},
        "light_level": {"initial": 400, "min": 0, "max": 1000, "fluctuation": 20},
        "air_quality": {"initial": 85, "min": 20, "max": 100, "fluctuation": 2.0},
    },
    "device_effects": {
        "light_on_brightness_delta": 50,
        "curtain_open_light_delta": 200,
        "fan_on_air_quality_delta": -5,
        "humidifier_on_humidity_delta": 2,
    },
    "alert_thresholds": {
        "temp_high": 30,
        "temp_low": 15,
        "air_quality_poor": 50,
    },
}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[环境配置] JSON 加载失败，使用默认值: {e}")
    return _DEFAULT_CONFIG


class SensorSimulator:
    def __init__(self, registry: DeviceRegistry):
        self.registry = registry
        self.config = _load_config()
        sc = self.config.get("sensors", {})
        self.values = {
            "temp_outdoor": 28.0,
            "temp_indoor": sc.get("temp_indoor", {}).get("initial", 25),
            "humidity": sc.get("humidity", {}).get("initial", 50),
            "light_level": sc.get("light_level", {}).get("initial", 400),
            "motion_living": sc.get("motion_living", {}).get("initial", True),
            "motion_bedroom": sc.get("motion_bedroom", {}).get("initial", False),
            "air_quality": sc.get("air_quality", {}).get("initial", 85),
        }
        self._listeners: list[Callable[[dict], None]] = []
        self._running = False

    def on_update(self, cb: Callable[[dict], None]):
        self._listeners.append(cb)

    def _notify(self):
        for cb in self._listeners:
            try:
                cb(dict(self.values))
            except Exception:
                pass

    def _current_hour(self) -> float:
        lt = time.localtime()
        return lt.tm_hour + lt.tm_min / 60.0

    def _daylight_factor(self) -> float:
        """根据时刻计算自然光照系数（0~1）。"""
        h = self._current_hour()
        return max(0.0, math.sin(math.pi * (h - 5.0) / 14.0)) if 5 <= h <= 19 else 0.0

    def tick(self):
        """推进一个仿真步（约每 2 秒调用一次）。"""
        v = self.values
        hour = self._current_hour()
        effects = self.config.get("device_effects", {})
        sc = self.config.get("sensors", {})

        # 自然光
        daylight = self._daylight_factor()
        v["light_level"] = daylight * 400.0

        # 灯具联动
        light = self.registry.get("light_living_01")
        if light and light.get("on"):
            brightness = (light.get("brightness") or 0) / 100.0
            v["light_level"] = max(v["light_level"], 150 + brightness * 300)

        # 窗帘联动
        curtain = self.registry.get("curtain_living_01")
        if curtain:
            pos = (curtain.get("position") or 0) / 100.0
            v["light_level"] += daylight * effects.get("curtain_open_light_delta", 200) * pos

        # 温度
        ac = self.registry.get("ac_living_01")
        target_temp = 26.0
        if ac and ac.get("on"):
            target_temp = float(ac.get("temperature") or 26)
        if ac and ac.get("on"):
            v["temp_indoor"] += (target_temp - v["temp_indoor"]) * 0.02
        else:
            v["temp_indoor"] += (v["temp_outdoor"] - v["temp_indoor"]) * 0.004
        v["temp_outdoor"] = 28.0 + 2.5 * math.sin((hour - 9.0) / 24.0 * 2 * math.pi)

        # 湿度
        fluct = sc.get("humidity", {}).get("fluctuation", 1.0)
        v["humidity"] += random.uniform(-fluct, fluct)
        humid = self.registry.get("humidifier_living_01")
        if humid and humid.get("on"):
            target_h = float(humid.get("target") or 55)
            v["humidity"] += (target_h - v["humidity"]) * 0.05
        h_min = sc.get("humidity", {}).get("min", 20)
        h_max = sc.get("humidity", {}).get("max", 90)
        v["humidity"] = max(h_min, min(h_max, v["humidity"]))

        # 人体红外
        if random.random() < 0.1:
            v["motion_living"] = not v["motion_living"]
        if random.random() < 0.05:
            v["motion_bedroom"] = not v["motion_bedroom"]

        # 空气质量
        aq_fluct = sc.get("air_quality", {}).get("fluctuation", 2.0)
        v["air_quality"] += random.uniform(-aq_fluct, aq_fluct)
        fan = self.registry.get("fan_living_01")
        if fan and fan.get("on"):
            v["air_quality"] += effects.get("fan_on_air_quality_delta", -5) * 0.1
        aq_min = sc.get("air_quality", {}).get("min", 20)
        aq_max = sc.get("air_quality", {}).get("max", 100)
        v["air_quality"] = max(aq_min, min(aq_max, v["air_quality"]))

        self._notify()

    async def run(self, interval: float = 2.0):
        self._running = True
        while self._running:
            self.tick()
            await asyncio.sleep(interval)

    def stop(self):
        self._running = False
