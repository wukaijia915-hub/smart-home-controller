# -*- coding: utf-8 -*-
"""工具 schema 生成器：从设备属性元数据反推工具契约。

设计原则 —— **schema 永不漂移**：

    命令名取自 devices.json 的 commands 列表；
    参数名、类型、取值范围取自 Device.attributes 的 (type, min, max, options)；
    两者都不手写常量。设备配置一改，工具契约自动跟着改。

    取代旧的 backend/tools.py：那里手写 schema 且参数名与
    virtual_devices.apply_command 实际读取的键名系统性漂移
    （power/target_temp/target_humidity/medium ↔ on/temperature/target/mid），
    导致即使补上执行体，大部分调用仍会静默走到 apply_command 的 else 分支。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from ..devices.schema import Device
from ..devices.virtual_devices import DEVICE_COMMAND_MAP

# JSON Schema / OpenAI Function Calling 的类型映射
_TYPE_MAP = {"number": "number", "bool": "boolean", "enum": "string", "text": "string"}

# 命令 → [(参数名, 属性名)]
# 参数名必须与 virtual_devices.apply_command 中 params.get(...) 读取的键名一致；
# 属性名是 Device.attributes 里的键（取值范围与单位从它反推）。
# 注意 set_clean_mode 的参数叫 mode 但属性叫 clean_mode，这类不一致由本表显式吸收。
PARAM_SOURCE: dict[str, list[tuple[str, str]]] = {
    "set_brightness":  [("brightness", "brightness")],
    "set_color_temp":  [("color_temp", "color_temp")],
    "set_position":    [("position", "position")],
    "set_temperature": [("temperature", "temperature")],
    "set_mode":        [("mode", "mode")],
    "set_fan_speed":   [("fan_speed", "fan_speed")],
    "set_volume":      [("volume", "volume")],
    "set_speed":       [("speed", "speed")],
    "set_target":      [("target", "target")],
    "set_lock_mode":   [("mode", "mode")],
    "set_clean_mode":  [("mode", "clean_mode")],
    "set_fan_level":   [("level", "fan_level")],
    "set_flame_level": [("level", "flame_level")],
    "play_music":      [("track", "track")],
    "take_photo":      [("timestamp", "last_photo")],
}

# 这些参数可省略（apply_command 中有默认值）
OPTIONAL_PARAMS: dict[str, set[str]] = {
    "play_music": {"track"},
    "take_photo": {"timestamp"},
}

# 命令的中文语义（用于工具描述与决策链路可读性）
COMMAND_LABELS: dict[str, str] = {
    "turn_on": "开启",
    "turn_off": "关闭",
    "set_brightness": "设置亮度",
    "set_color_temp": "设置色温",
    "open": "打开",
    "close": "关闭",
    "set_position": "设置开合度",
    "set_temperature": "设置温度",
    "set_mode": "设置模式",
    "set_fan_speed": "设置风速",
    "set_volume": "设置音量",
    "next_channel": "下一个频道",
    "prev_channel": "上一个频道",
    "set_speed": "设置档位",
    "set_target": "设置目标湿度",
    "lock": "上锁",
    "unlock": "解锁",
    "set_lock_mode": "设置门锁模式",
    "start_clean": "开始清扫",
    "stop_clean": "停止清扫",
    "return_dock": "返回充电座",
    "set_clean_mode": "设置清洁模式",
    "set_fan_level": "设置风速档位",
    "set_flame_level": "设置火力档位",
    "start_stream": "开启直播",
    "stop_stream": "停止直播",
    "take_photo": "拍照",
    "play_music": "播放音乐",
    "stop_music": "停止播放",
}


@dataclass
class ToolParameter:
    """工具参数契约。"""
    name: str
    type: str = "string"          # number / boolean / string
    required: bool = True
    description: str = ""
    enum: list = field(default_factory=list)   # type=string 且受限时的可选值
    min: Optional[float] = None
    max: Optional[float] = None
    unit: str = ""


@dataclass
class Tool:
    """一个可调用的工具。

    - 设备类工具：由 `device_id` + `command` 唯一确定动作，走 DeviceRegistry 执行
    - 外部工具（场景/系统/知识/元工具）：提供 `handler`，由 handler 自执行
    """
    name: str
    description: str
    category: str = "device"      # device / scene / system / knowledge / meta
    parameters: list[ToolParameter] = field(default_factory=list)
    device_id: Optional[str] = None
    command: Optional[str] = None
    handler: Optional[Callable] = field(default=None, repr=False, compare=False)

    def to_schema(self) -> dict:
        """输出 OpenAI Function Calling 兼容的 schema。"""
        props: dict = {}
        required: list[str] = []
        for p in self.parameters:
            prop: dict = {"type": p.type, "description": p.description}
            if p.enum:
                prop["enum"] = p.enum
            if p.min is not None:
                prop["minimum"] = p.min
            if p.max is not None:
                prop["maximum"] = p.max
            props[p.name] = prop
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "device_id": self.device_id,
            "command": self.command,
            "parameters": [asdict(p) for p in self.parameters],
        }


def tool_name(device_id: str, command: str) -> str:
    """工具全名：`<device_id>__<command>`。

    用设备 id 而非房间名 —— 旧实现按房间命名（set_客厅_light），
    同房间多台同类设备会互相覆盖（客厅两盏灯只剩一个工具）。
    双下划线分隔符同时兼容 OpenAI Function Calling 的命名约束。
    """
    return f"{device_id}__{command}"


def _param_description(attr) -> str:
    parts = [attr.label or attr.name]
    if attr.type == "enum" and attr.options:
        parts.append(f"可选：{'/'.join(str(o) for o in attr.options)}")
    elif attr.type == "number":
        if attr.min is not None and attr.max is not None:
            parts.append(f"范围 {attr.min}-{attr.max}{attr.unit}")
        elif attr.unit:
            parts.append(f"单位 {attr.unit}")
    elif attr.type == "bool":
        parts.append("布尔值")
    return "；".join(parts)


def tool_for(device: Device, command: str) -> Tool:
    """为 `device` 的 `command` 生成工具契约（参数从设备属性元数据反推）。"""
    params: list[ToolParameter] = []
    optional = OPTIONAL_PARAMS.get(command, set())
    for pname, aname in PARAM_SOURCE.get(command, []):
        attr = device.attributes.get(aname)
        if attr is None:
            # 设备没有这个属性 → 该命令实际上执行不了，跳过而不是编一个假 schema
            continue
        params.append(ToolParameter(
            name=pname,
            type=_TYPE_MAP.get(attr.type, "string"),
            required=pname not in optional,
            description=_param_description(attr),
            enum=list(attr.options) if (attr.type == "enum" and attr.options) else [],
            min=attr.min if attr.type == "number" else None,
            max=attr.max if attr.type == "number" else None,
            unit=attr.unit or "",
        ))
    label = COMMAND_LABELS.get(command, command)
    return Tool(
        name=tool_name(device.device_id, command),
        description=f"{device.name}（{device.room}）— {label}",
        category="device",
        parameters=params,
        device_id=device.device_id,
        command=command,
    )


def build_tools(devices) -> list[Tool]:
    """为一组设备生成全部工具。

    只生成「设备声明的命令 ∩ 该类别实际实现（DEVICE_COMMAND_MAP）里的命令」，
    避免造出 apply_command 会拒绝的假工具。
    """
    tools: list[Tool] = []
    for device in devices:
        implemented = DEVICE_COMMAND_MAP.get(device.category, set())
        for command in device.commands:
            if command in implemented:
                tools.append(tool_for(device, command))
    return tools
