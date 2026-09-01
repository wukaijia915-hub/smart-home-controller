"""标准化工具注册表：将设备控制抽象为统一 Tool 接口，支持工具发现、枚举、直接调用。
参考 openvela AI Agent 的 tool_registry 设计，提供可审计的工具调用链。
"""
from __future__ import annotations
import json, os, logging
from typing import Callable, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("tools")


@dataclass
class ToolParameter:
    name: str
    type: str  # string / number / boolean / enum
    required: bool = True
    description: str = ""
    enum: list = field(default_factory=list)  # type=enum 时的可选值
    min: Optional[float] = None
    max: Optional[float] = None


@dataclass
class Tool:
    name: str
    description: str
    category: str  # device / scene / system / knowledge
    parameters: list[ToolParameter] = field(default_factory=list)
    handler: Optional[Callable] = None
    device_id: Optional[str] = None  # 关联的设备 ID

    def to_schema(self) -> dict:
        """输出 OpenAI Function Calling 兼容的 schema。"""
        props = {}
        required = []
        for p in self.parameters:
            prop = {"type": p.type, "description": p.description}
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
            "parameters": [asdict(p) for p in self.parameters],
        }


class ToolRegistry:
    """全局工具注册表。"""
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._call_log: list[dict] = []  # 工具调用日志（可审计）

    def register(self, tool: Tool):
        self._tools[tool.name] = tool
        logger.debug(f"注册工具: {tool.name} ({tool.category})")

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self, category: str = None) -> list[Tool]:
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        return tools

    def list_schemas(self) -> list[dict]:
        """输出所有工具的 OpenAI Function Calling schema。"""
        return [t.to_schema() for t in self._tools.values()]

    def call(self, name: str, params: dict = None) -> dict:
        """直接调用工具（不经 LLM），返回 {ok, result, error}。"""
        params = params or {}
        tool = self._tools.get(name)
        if not tool:
            return {"ok": False, "error": f"未知工具: {name}", "supported": list(self._tools.keys())}
        # 参数校验
        for p in tool.parameters:
            if p.required and p.name not in params:
                return {"ok": False, "error": f"缺少必填参数: {p.name}"}
            if p.enum and params.get(p.name) not in p.enum:
                return {"ok": False, "error": f"参数 {p.name} 无效，可选值: {p.enum}"}
            if p.min is not None and params.get(p.name, 0) < p.min:
                return {"ok": False, "error": f"参数 {p.name} 超出下限: {p.min}"}
            if p.max is not None and params.get(p.name, 0) > p.max:
                return {"ok": False, "error": f"参数 {p.name} 超出上限: {p.max}"}
        # 执行
        try:
            result = tool.handler(params) if tool.handler else {"status": "noop"}
            log_entry = {"tool": name, "params": params, "result": result, "ok": True}
            self._call_log.append(log_entry)
            if len(self._call_log) > 200:
                self._call_log.pop(0)
            return {"ok": True, "result": result, "tool": name}
        except Exception as e:
            logger.error(f"工具调用失败 {name}: {e}")
            return {"ok": False, "error": str(e), "tool": name}

    def get_call_log(self, limit: int = 50) -> list[dict]:
        return self._call_log[-limit:]

    def build_from_devices(self, registry):
        """从设备注册表自动构建设备控制工具。"""
        from .devices.schema import DeviceCategory
        for dev in registry.all():
            cat = dev.category
            if cat == DeviceCategory.LIGHT:
                self._register_light_tools(dev)
            elif cat == DeviceCategory.CURTAIN:
                self._register_curtain_tools(dev)
            elif cat == DeviceCategory.AC:
                self._register_ac_tools(dev)
            elif cat == DeviceCategory.TV:
                self._register_tv_tools(dev)
            elif cat == DeviceCategory.FAN:
                self._register_fan_tools(dev)
            elif cat == DeviceCategory.HUMIDIFIER:
                self._register_humidifier_tools(dev)
            elif cat == DeviceCategory.LOCK:
                self._register_lock_tools(dev)
            elif cat == DeviceCategory.ROBOT_VACUUM:
                self._register_robot_tools(dev)
            elif cat == DeviceCategory.RANGE_HOOD:
                self._register_range_hood_tools(dev)
            elif cat == DeviceCategory.CAMERA:
                self._register_camera_tools(dev)
            elif cat == DeviceCategory.SPEAKER:
                self._register_speaker_tools(dev)

    def _register_light_tools(self, dev):
        room = dev.room
        self.register(Tool(
            name=f"set_{room}_light",
            description=f"控制{dev.name}的开关、亮度和色温",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("power", "string", enum=["on", "off"], description="开关状态"),
                ToolParameter("brightness", "number", required=False, min=0, max=100, description="亮度 0-100"),
                ToolParameter("color_temp", "number", required=False, min=2700, max=6500, description="色温 K"),
            ],
            handler=lambda p: dev.execute(p),
        ))

    def _register_curtain_tools(self, dev):
        room = dev.room
        self.register(Tool(
            name=f"set_{room}_curtain",
            description=f"控制{dev.name}的开合度",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("position", "number", min=0, max=100, description="开合度 0-100（0=全关，100=全开）"),
            ],
            handler=lambda p: dev.execute({"position": p["position"]}),
        ))

    def _register_ac_tools(self, dev):
        room = dev.room
        self.register(Tool(
            name=f"set_{room}_ac",
            description=f"控制{dev.name}的开关、模式、温度和风速",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("power", "string", enum=["on", "off"], description="开关状态"),
                ToolParameter("mode", "string", required=False, enum=["cool", "heat", "fan", "auto"], description="运行模式"),
                ToolParameter("target_temp", "number", required=False, min=16, max=30, description="目标温度 16-30℃"),
                ToolParameter("fan_speed", "string", required=False, enum=["low", "medium", "high", "auto"], description="风速"),
            ],
            handler=lambda p: dev.execute(p),
        ))

    def _register_tv_tools(self, dev):
        self.register(Tool(
            name=f"set_{dev.room}_tv",
            description=f"控制{dev.name}的开关、音量和频道",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("power", "string", enum=["on", "off"], description="开关状态"),
                ToolParameter("volume", "number", required=False, min=0, max=100, description="音量 0-100"),
                ToolParameter("channel", "number", required=False, min=1, max=999, description="频道号"),
            ],
            handler=lambda p: dev.execute(p),
        ))

    def _register_fan_tools(self, dev):
        self.register(Tool(
            name=f"set_{dev.room}_fan",
            description=f"控制{dev.name}的开关和档位",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("power", "string", enum=["on", "off"], description="开关状态"),
                ToolParameter("speed", "number", required=False, min=1, max=3, description="档位 1-3"),
            ],
            handler=lambda p: dev.execute(p),
        ))

    def _register_humidifier_tools(self, dev):
        self.register(Tool(
            name=f"set_{dev.room}_humidifier",
            description=f"控制{dev.name}的开关和目标湿度",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("power", "string", enum=["on", "off"], description="开关状态"),
                ToolParameter("target_humidity", "number", required=False, min=30, max=80, description="目标湿度 %"),
            ],
            handler=lambda p: dev.execute(p),
        ))

    def _register_lock_tools(self, dev):
        self.register(Tool(
            name=f"set_{dev.room}_lock",
            description=f"控制{dev.name}的锁定状态和模式",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("action", "string", enum=["lock", "unlock"], description="操作"),
                ToolParameter("mode", "string", required=False, enum=["normal", "away", "vacation"], description="门锁模式"),
            ],
            handler=lambda p: dev.execute({"command": p["action"], "params": p}),
        ))

    def _register_robot_tools(self, dev):
        self.register(Tool(
            name=f"control_{dev.room}_robot",
            description=f"控制{dev.name}的清洁任务",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("action", "string", enum=["start_clean", "stop_clean", "return_dock"], description="操作"),
                ToolParameter("clean_mode", "string", required=False, enum=["auto", "edge", "spot", "deep"], description="清洁模式"),
            ],
            handler=lambda p: dev.execute({"command": p["action"], "params": p}),
        ))

    def _register_range_hood_tools(self, dev):
        self.register(Tool(
            name=f"set_{dev.room}_range_hood",
            description=f"控制{dev.name}的开关和风速",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("power", "string", enum=["on", "off"], description="开关状态"),
                ToolParameter("fan_level", "number", required=False, min=1, max=4, description="风速档位 1-4"),
            ],
            handler=lambda p: dev.execute(p),
        ))

    def _register_camera_tools(self, dev):
        self.register(Tool(
            name=f"control_{dev.room}_camera",
            description=f"控制{dev.name}的直播和拍照",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("action", "string", enum=["start_stream", "stop_stream", "take_photo"], description="操作"),
            ],
            handler=lambda p: dev.execute({"command": p["action"], "params": {}}),
        ))

    def _register_speaker_tools(self, dev):
        self.register(Tool(
            name=f"control_{dev.room}_speaker",
            description=f"控制{dev.name}的音乐播放和音量",
            category="device",
            device_id=dev.device_id,
            parameters=[
                ToolParameter("action", "string", enum=["play_music", "stop_music", "turn_on", "turn_off"], description="操作"),
                ToolParameter("volume", "number", required=False, min=0, max=100, description="音量 0-100"),
                ToolParameter("track", "string", required=False, description="曲目名称"),
            ],
            handler=lambda p: dev.execute({"command": p["action"], "params": p}),
        ))


# 全局单例
tool_registry = ToolRegistry()
