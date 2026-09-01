# -*- coding: utf-8 -*-
"""设备注册表：设备管理、命令执行、状态快照与变更事件发布。"""
from __future__ import annotations

from typing import Callable, Optional

from .schema import Device
from .virtual_devices import build_default_devices, apply_command


class DeviceRegistry:
    def __init__(self):
        self.devices: dict[str, Device] = {}
        self._listeners: list[Callable[[Device, str, dict], None]] = []
        for d in build_default_devices():
            self.devices[d.device_id] = d

    def on_change(self, cb: Callable[[Device, str, dict], None]):
        """注册设备变更回调：cb(device, command, result)"""
        self._listeners.append(cb)

    def get(self, device_id: str) -> Optional[Device]:
        return self.devices.get(device_id)

    def all(self) -> list[Device]:
        return list(self.devices.values())

    def snapshot(self) -> list[dict]:
        return [d.to_dict() for d in self.devices.values()]

    def control(self, device_id: str, command: str, params: Optional[dict] = None) -> dict:
        """执行设备控制命令，返回执行结果。"""
        params = params or {}
        device = self.devices.get(device_id)
        if device is None:
            return {"ok": False, "error": f"设备不存在: {device_id}"}
        if device.state == "offline":
            return {"ok": False, "error": f"设备 {device.name} 已离线"}
        if device.state == "fault":
            return {"ok": False, "error": f"设备 {device.name} 发生故障"}
        ok = apply_command(device, command, params)
        result = {"ok": ok, "device_id": device_id, "command": command, "params": params}
        if ok:
            result["state"] = device.to_dict()
        else:
            result["error"] = f"命令 {command} 不受设备 {device.name} 支持"
        for cb in self._listeners:
            try:
                cb(device, command, result)
            except Exception:
                pass
        return result

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
