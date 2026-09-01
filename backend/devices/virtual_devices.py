# -*- coding: utf-8 -*-
"""虚拟设备工厂：定义 10+ 类异构设备的初始状态与命令处理。
纯软件阶段以"虚拟设备驱动"模拟 WiFi/BLE/Zigbee/Matter 协议差异。
支持从 config/devices.json 加载设备清单（JSON 配置驱动）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .schema import Device, Attribute, DeviceCategory

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "devices.json"


def _light(device_id: str, name: str, room: str, brightness: int = 0,
           color_temp: int = 4000) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.LIGHT, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="开关"),
            "brightness": Attribute("brightness", brightness, "number", min=0, max=100, unit="%", label="亮度"),
            "color_temp": Attribute("color_temp", color_temp, "number", min=2700, max=6500, unit="K", label="色温"),
        },
        commands=["turn_on", "turn_off", "set_brightness", "set_color_temp"],
    )


def _curtain(device_id: str, name: str, room: str, position: int = 0) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.CURTAIN, room=room,
        attributes={
            "on": Attribute("on", position > 0, "bool", label="开启"),
            "position": Attribute("position", position, "number", min=0, max=100, unit="%", label="开合度"),
        },
        commands=["open", "close", "set_position"],
    )


def _ac(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.AC, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="开关"),
            "mode": Attribute("mode", "cool", "enum", options=["cool", "heat", "fan", "auto"], label="模式"),
            "temperature": Attribute("temperature", 26, "number", min=16, max=30, unit="℃", label="设定温度"),
            "fan_speed": Attribute("fan_speed", "auto", "enum", options=["low", "mid", "high", "auto"], label="风速"),
        },
        commands=["turn_on", "turn_off", "set_temperature", "set_mode", "set_fan_speed"],
    )


def _tv(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.TV, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="开关"),
            "volume": Attribute("volume", 20, "number", min=0, max=100, unit="%", label="音量"),
            "channel": Attribute("channel", 1, "number", min=1, max=100, label="频道"),
        },
        commands=["turn_on", "turn_off", "set_volume", "next_channel", "prev_channel"],
    )


def _fan(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.FAN, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="开关"),
            "speed": Attribute("speed", 2, "enum", options=["low", "mid", "high"], label="档位"),
        },
        commands=["turn_on", "turn_off", "set_speed"],
    )


def _humidifier(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.HUMIDIFIER, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="开关"),
            "target": Attribute("target", 55, "number", min=30, max=80, unit="%", label="目标湿度"),
        },
        commands=["turn_on", "turn_off", "set_target"],
    )


def _lock(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.LOCK, room=room,
        attributes={
            "locked": Attribute("locked", True, "bool", label="锁定"),
            "mode": Attribute("mode", "normal", "enum", options=["normal", "away", "vacation"], label="模式"),
            "battery": Attribute("battery", 85, "number", min=0, max=100, unit="%", label="电量"),
        },
        commands=["lock", "unlock", "set_lock_mode"],
    )


def _robot_vacuum(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.ROBOT_VACUUM, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="运行"),
            "status": Attribute("status", "idle", "enum", options=["idle", "cleaning", "charging", "paused"], label="状态"),
            "battery": Attribute("battery", 90, "number", min=0, max=100, unit="%", label="电量"),
            "clean_mode": Attribute("clean_mode", "auto", "enum", options=["auto", "edge", "spot", "deep"], label="清洁模式"),
        },
        commands=["start_clean", "stop_clean", "return_dock", "set_clean_mode"],
    )


def _range_hood(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.RANGE_HOOD, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="开关"),
            "fan_level": Attribute("fan_level", 0, "number", min=0, max=4, label="风速档位"),
        },
        commands=["turn_on", "turn_off", "set_fan_level"],
    )


def _camera(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.CAMERA, room=room,
        attributes={
            "streaming": Attribute("streaming", False, "bool", label="直播中"),
            "last_photo": Attribute("last_photo", 0, "number", label="最近拍照时间"),
            "motion_detected": Attribute("motion_detected", False, "bool", label="移动侦测"),
        },
        commands=["start_stream", "stop_stream", "take_photo"],
    )


def _speaker(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.SPEAKER, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="开关"),
            "playing": Attribute("playing", False, "bool", label="播放中"),
            "volume": Attribute("volume", 30, "number", min=0, max=100, unit="%", label="音量"),
            "track": Attribute("track", "", "text", label="当前曲目"),
        },
        commands=["turn_on", "turn_off", "play_music", "stop_music", "set_volume"],
    )


def _gas_stove(device_id: str, name: str, room: str) -> Device:
    return Device(
        device_id=device_id, name=name, category=DeviceCategory.GAS_STOVE, room=room,
        attributes={
            "on": Attribute("on", False, "bool", label="点火"),
            "flame_level": Attribute("flame_level", 0, "number", min=0, max=5, label="火力档位"),
        },
        commands=["turn_on", "turn_off", "set_flame_level"],
    )


def _device_from_config(cfg: dict) -> Device:
    """从 JSON 配置项创建 Device 对象。"""
    attrs = {}
    for key, acfg in cfg.get("attributes", {}).items():
        attrs[key] = Attribute(
            name=key,
            value=acfg.get("value"),
            type=acfg.get("type", "bool"),
            min=acfg.get("min"),
            max=acfg.get("max"),
            unit=acfg.get("unit", ""),
            options=acfg.get("options"),
            label=acfg.get("label", key),
        )
    return Device(
        device_id=cfg["device_id"],
        name=cfg["name"],
        category=cfg["category"],
        room=cfg.get("room", ""),
        attributes=attrs,
        commands=cfg.get("commands", []),
        protocol=cfg.get("protocol", "virtual"),
    )


def build_default_devices() -> list[Device]:
    """初始设备清单：优先从 config/devices.json 加载，不存在则回退硬编码。"""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            devices = [_device_from_config(c) for c in data.get("devices", [])]
            if devices:
                return devices
        except Exception as e:
            print(f"[设备配置] JSON 加载失败，回退硬编码: {e}")
    # 回退：硬编码默认设备
    return [
        # 客厅
        _light("light_living_01", "客厅吊灯", "客厅", brightness=0),
        _light("light_living_02", "客厅台灯", "客厅", brightness=0, color_temp=3500),
        _curtain("curtain_living_01", "客厅窗帘", "客厅", position=0),
        _ac("ac_living_01", "客厅空调", "客厅"),
        _tv("tv_living_01", "客厅电视", "客厅"),
        _fan("fan_living_01", "客厅风扇", "客厅"),
        _humidifier("humidifier_living_01", "客厅加湿器", "客厅"),
        # 卧室
        _light("light_bedroom_01", "卧室吸顶灯", "卧室", brightness=0),
        _curtain("curtain_bedroom_01", "卧室窗帘", "卧室", position=0),
        _ac("ac_bedroom_01", "卧室空调", "卧室"),
    ]


DEVICE_COMMAND_MAP = {
    DeviceCategory.LIGHT: {"turn_on", "turn_off", "set_brightness", "set_color_temp"},
    DeviceCategory.CURTAIN: {"open", "close", "set_position"},
    DeviceCategory.AC: {"turn_on", "turn_off", "set_temperature", "set_mode", "set_fan_speed"},
    DeviceCategory.TV: {"turn_on", "turn_off", "set_volume", "next_channel", "prev_channel"},
    DeviceCategory.FAN: {"turn_on", "turn_off", "set_speed"},
    DeviceCategory.HUMIDIFIER: {"turn_on", "turn_off", "set_target"},
    DeviceCategory.LOCK: {"lock", "unlock", "set_lock_mode"},
    DeviceCategory.ROBOT_VACUUM: {"start_clean", "stop_clean", "return_dock", "set_clean_mode"},
    DeviceCategory.RANGE_HOOD: {"turn_on", "turn_off", "set_fan_level"},
    DeviceCategory.CAMERA: {"start_stream", "stop_stream", "take_photo"},
    DeviceCategory.SPEAKER: {"turn_on", "turn_off", "play_music", "stop_music", "set_volume"},
    DeviceCategory.GAS_STOVE: {"turn_on", "turn_off", "set_flame_level"},
}


def apply_command(device: Device, command: str, params: dict) -> bool:
    """在虚拟设备上执行命令，返回是否成功。"""
    if device.state != "online":
        return False
    allowed = DEVICE_COMMAND_MAP.get(device.category, set())
    if command not in allowed:
        return False

    if command == "turn_on":
        device.set("on", True)
        # 灯具开灯时若亮度为0，自动恢复默认亮度
        if device.category == DeviceCategory.LIGHT:
            br = device.get("brightness")
            if br is None or br == 0:
                device.set("brightness", 80)
    elif command == "turn_off":
        device.set("on", False)
    elif command == "set_brightness":
        device.set("on", True)
        device.set("brightness", int(params.get("brightness", 80)))
    elif command == "set_color_temp":
        device.set("on", True)
        device.set("color_temp", int(params.get("color_temp", 4000)))
    elif command == "open":
        device.set("on", True)
        device.set("position", 100)
    elif command == "close":
        device.set("position", 0)
        device.set("on", False)
    elif command == "set_position":
        pos = int(params.get("position", 100))
        device.set("position", pos)
        device.set("on", pos > 0)
    elif command == "set_temperature":
        device.set("on", True)
        device.set("temperature", int(params.get("temperature", 26)))
    elif command == "set_mode":
        device.set("on", True)
        device.set("mode", params.get("mode", "auto"))
    elif command == "set_fan_speed":
        device.set("on", True)
        device.set("fan_speed", params.get("fan_speed", "auto"))
    elif command == "set_volume":
        device.set("on", True)
        device.set("volume", int(params.get("volume", 20)))
    elif command == "next_channel":
        device.set("on", True)
        device.set("channel", min(100, device.get("channel") + 1))
    elif command == "prev_channel":
        device.set("on", True)
        device.set("channel", max(1, device.get("channel") - 1))
    elif command == "set_speed":
        device.set("on", True)
        device.set("speed", params.get("speed", "mid"))
    elif command == "set_target":
        device.set("on", True)
        device.set("target", int(params.get("target", 55)))
    # ---- 智能门锁 ----
    elif command == "lock":
        device.set("locked", True)
    elif command == "unlock":
        device.set("locked", False)
    elif command == "set_lock_mode":
        device.set("mode", params.get("mode", "normal"))
    # ---- 扫地机器人 ----
    elif command == "start_clean":
        device.set("on", True)
        device.set("status", "cleaning")
    elif command == "stop_clean":
        device.set("on", False)
        device.set("status", "idle")
    elif command == "return_dock":
        device.set("on", False)
        device.set("status", "charging")
        device.set("battery", min(100, device.get("battery") + 1))
    elif command == "set_clean_mode":
        device.set("clean_mode", params.get("mode", "auto"))
    # ---- 抽油烟机 ----
    elif command == "set_fan_level":
        device.set("on", True)
        device.set("fan_level", int(params.get("level", 2)))
    # ---- 燃气灶 ----
    elif command == "set_flame_level":
        device.set("on", True)
        device.set("flame_level", int(params.get("level", 2)))
    # ---- 摄像头 ----
    elif command == "start_stream":
        device.set("streaming", True)
    elif command == "stop_stream":
        device.set("streaming", False)
    elif command == "take_photo":
        device.set("last_photo", int(params.get("timestamp", 0)))
    # ---- 智能音箱 ----
    elif command == "play_music":
        device.set("on", True)
        device.set("playing", True)
        device.set("track", params.get("track", "默认歌单"))
    elif command == "stop_music":
        device.set("playing", False)
    else:
        return False
    return True
