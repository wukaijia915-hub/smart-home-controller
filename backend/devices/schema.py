# -*- coding: utf-8 -*-
"""设备抽象模型：Device / Attribute / Command / State。
参考 Matter cluster/attribute 与 Home Assistant entity 模型设计。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


class DeviceCategory:
    LIGHT = "light"
    CURTAIN = "curtain"
    AC = "ac"
    TV = "tv"
    FAN = "fan"
    HUMIDIFIER = "humidifier"
    LOCK = "lock"
    ROBOT_VACUUM = "robot_vacuum"
    RANGE_HOOD = "range_hood"
    CAMERA = "camera"
    SPEAKER = "speaker"
    GAS_STOVE = "gas_stove"


@dataclass
class Attribute:
    """设备属性：类型、范围、单位、可选项。"""
    name: str
    value: Any
    type: str = "number"            # number | bool | enum | text
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    options: Optional[list] = None  # enum 可选项
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "type": self.type,
            "unit": self.unit,
            "min": self.min,
            "max": self.max,
            "options": self.options,
            "label": self.label,
        }


@dataclass
class Device:
    device_id: str
    name: str
    category: str
    room: str
    attributes: dict = field(default_factory=dict)   # name -> Attribute
    commands: list = field(default_factory=list)     # 可用命令
    state: str = "online"                            # online / offline / fault
    protocol: str = "virtual"                        # wifi / ble / zigbee / matter / virtual

    def get(self, name: str) -> Any:
        attr = self.attributes.get(name)
        return attr.value if attr else None

    def set(self, name: str, value: Any) -> bool:
        attr = self.attributes.get(name)
        if attr is None:
            return False
        # 数值范围校验
        if attr.type == "number":
            if attr.min is not None and value < attr.min:
                value = attr.min
            if attr.max is not None and value > attr.max:
                value = attr.max
        elif attr.type == "enum":
            if attr.options and value not in attr.options:
                return False
        attr.value = value
        return True

    def to_dict(self, full: bool = True) -> dict:
        d = {
            "device_id": self.device_id,
            "name": self.name,
            "category": self.category,
            "room": self.room,
            "state": self.state,
            "commands": self.commands,
        }
        d["attributes"] = {k: a.to_dict() for k, a in self.attributes.items()}
        return d
