# -*- coding: utf-8 -*-
"""设备注册表：设备管理、命令执行、状态快照与变更事件发布。

执行状态契约（证据链闭环）：
- 命令成功后回读关键属性，比对目标值 → status=confirmed；
- 无法回读的命令 → status=delivered；
- 失败 → failed / error。严格区分「请求已发出」与「动作已完成」，
  抑制大模型幻觉误动作（三段式状态设计参考 UAgentOS ActionResult，Apache-2.0）。
- 孪生同步：若设备存在孪生对（可执行数字孪生），回读结果同步写入实体侧
  占位适配器，形成「虚拟预期态 ↔ 实体实测态」一致性证据。
"""
from __future__ import annotations

from typing import Callable, Optional

from .schema import Device
from .virtual_devices import build_default_devices, apply_command

# 命令 → 回读属性：执行成功后读取该属性并与目标值比对
READBACK_ATTR = {
    "turn_on": "on", "turn_off": "on",
    "set_brightness": "brightness",
    "set_color_temp": "color_temp",
    "open": "position", "close": "position", "set_position": "position",
    "set_temperature": "temperature",
    "set_mode": "mode",
    "set_fan_speed": "fan_speed",
    "set_volume": "volume",
    "set_speed": "speed",
    "set_target": "target",
    "lock": "locked", "unlock": "locked",
    "start_clean": "status", "stop_clean": "status",
    "set_flame_level": "flame_level",
    "set_fan_level": "fan_level",
    "play_music": "playing", "stop_music": "playing",
    "start_stream": "streaming", "stop_stream": "streaming",
}

# 命令 → 回读目标值来源（params 键名）
READBACK_PARAM = {
    "turn_on": None, "turn_off": None,      # 目标值固定 True/False
    "set_brightness": "brightness",
    "set_color_temp": "color_temp",
    "open": None, "close": None, "set_position": "position",
    "set_temperature": "temperature",
    "set_mode": "mode",
    "set_fan_speed": "fan_speed",
    "set_volume": "volume",
    "set_speed": "speed",
    "set_target": "target",
    "lock": None, "unlock": None,
    "start_clean": None, "stop_clean": None,
    "set_flame_level": "level",
    "set_fan_level": "level",
    "play_music": None, "stop_music": None,
    "start_stream": None, "stop_stream": None,
}


class DeviceRegistry:
    def __init__(self):
        self.devices: dict[str, Device] = {}
        self._listeners: list[Callable[[Device, str, dict], None]] = []
        self._twin_sync: Optional[Callable[[Device, dict], Optional[dict]]] = None
        for d in build_default_devices():
            self.devices[d.device_id] = d

    def on_change(self, cb: Callable[[Device, str, dict], None]):
        """注册设备变更回调：cb(device, command, result)"""
        self._listeners.append(cb)

    def set_twin_sync(self, cb: Callable[[Device, dict], Optional[dict]]):
        """注册孪生同步回调：cb(device, attrs) -> 实体侧回读证据（可为 None）。"""
        self._twin_sync = cb

    def get(self, device_id: str) -> Optional[Device]:
        return self.devices.get(device_id)

    def all(self) -> list[Device]:
        return list(self.devices.values())

    def snapshot(self) -> list[dict]:
        return [d.to_dict() for d in self.devices.values()]

    def control(self, device_id: str, command: str, params: Optional[dict] = None) -> dict:
        """执行设备控制命令，返回执行结果。

        result 新增字段（向后兼容）：
        - status: confirmed / delivered / failed / error
        - readback: {attribute, expected, actual, confirmed}
        - twin: 孪生对实体侧回读证据（仅孪生体设备）
        """
        params = params or {}
        device = self.devices.get(device_id)
        if device is None:
            return {"ok": False, "status": "error", "error": f"设备不存在: {device_id}"}
        if device.state == "offline":
            return {"ok": False, "status": "error", "error": f"设备 {device.name} 已离线"}
        if device.state == "fault":
            return {"ok": False, "status": "error", "error": f"设备 {device.name} 发生故障"}
        ok = apply_command(device, command, params)
        result = {"ok": ok, "device_id": device_id, "command": command, "params": params}
        if ok:
            result["state"] = device.to_dict()
            # ---- 回读确认：区分 confirmed / delivered ----
            readback = self._readback(device, command, params)
            result["status"] = "confirmed" if readback["confirmed"] else "delivered"
            result["readback"] = readback
            # ---- 孪生同步：虚拟预期态 → 实体占位适配器，回读一致性证据 ----
            if self._twin_sync is not None:
                attrs = {name: attr.value for name, attr in device.attributes.items()}
                twin_evidence = self._twin_sync(device, attrs)
                if twin_evidence is not None:
                    result["twin"] = twin_evidence
        else:
            result["status"] = "failed"
            result["error"] = f"命令 {command} 不受设备 {device.name} 支持"
        for cb in self._listeners:
            try:
                cb(device, command, result)
            except Exception:
                pass
        return result

    @staticmethod
    def _readback(device: Device, command: str, params: dict) -> dict:
        """执行后回读设备属性，与目标值比对，生成证据。"""
        attr_name = READBACK_ATTR.get(command)
        if not attr_name or attr_name not in device.attributes:
            return {"attribute": None, "expected": None, "actual": None, "confirmed": False}
        actual = device.get(attr_name)
        param_key = READBACK_PARAM.get(command)
        if param_key is None:
            # 目标值由命令语义决定：turn_on→True，open→100，close→0 等
            expected = _command_target_value(command)
        else:
            expected = params.get(param_key)
        confirmed = _values_equal(actual, expected)
        return {"attribute": attr_name, "expected": expected, "actual": actual,
                "confirmed": confirmed}

    def set_state(self, device_id: str, state: str):
        """模拟设备离线/故障/恢复（边缘场景）。"""
        device = self.devices.get(device_id)
        if device:
            device.state = state
            for cb in self._listeners:
                try:
                    cb(device, "__state__", {"ok": True, "state": state})
                except Exception:
                    pass

    def reset(self):
        """重置为默认状态。"""
        self.devices = {}
        for d in build_default_devices():
            self.devices[d.device_id] = d

    def reload(self, new_devices: list):
        """热重载设备清单：保留已有设备的当前状态，新增设备，移除不存在的设备。"""
        old = dict(self.devices)
        self.devices = {}
        for d in new_devices:
            if d.device_id in old:
                # 保留旧设备的当前状态
                d.state = old[d.device_id].state
                for k, v in old[d.device_id].attributes.items():
                    if k in d.attributes:
                        d.attributes[k].value = v.value
            self.devices[d.device_id] = d


def _command_target_value(command: str):
    """由命令语义确定回读目标值（on→布尔，position→数值，status→枚举字符串）。"""
    if command in ("turn_on", "lock", "start_clean", "play_music", "start_stream"):
        return True
    if command in ("turn_off", "unlock", "stop_music", "stop_stream"):
        return False
    if command == "open":
        return 100
    if command == "close":
        return 0
    if command == "start_clean":
        return "cleaning"
    if command == "stop_clean":
        return "idle"
    return None


def _values_equal(actual, expected) -> bool:
    """宽松数值比对（参数可能以 str 传入，属性为 int）。"""
    if actual == expected:
        return True
    try:
        return float(actual) == float(expected)
    except (TypeError, ValueError):
        return False


    def set_state(self, device_id: str, state: str):
        """模拟设备离线/故障/恢复（边缘场景）。"""
        device = self.devices.get(device_id)
        if device:
            device.state = state
            for cb in self._listeners:
                try:
                    cb(device, "__state__", {"ok": True, "state": state})
                except Exception:
                    pass

    def reset(self):
        """重置为默认状态。"""
        self.devices = {}
        for d in build_default_devices():
            self.devices[d.device_id] = d

    def reload(self, new_devices: list):
        """热重载设备清单：保留已有设备的当前状态，新增设备，移除不存在的设备。"""
        old = dict(self.devices)
        self.devices = {}
        for d in new_devices:
            if d.device_id in old:
                # 保留旧设备的当前状态
                d.state = old[d.device_id].state
                for k, v in old[d.device_id].attributes.items():
                    if k in d.attributes:
                        d.attributes[k].value = v.value
            self.devices[d.device_id] = d
