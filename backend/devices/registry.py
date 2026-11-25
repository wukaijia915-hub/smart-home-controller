# -*- coding: utf-8 -*-
"""设备注册表：设备管理、命令执行、状态快照与变更事件发布。

执行状态契约（证据链闭环）：
- 命令成功后回读关键属性，比对目标值 → status=confirmed；
- 无法回读 / 回读值不可信（unknown、unavailable）/ 与预期不符 → status=delivered，
  并在 `readback.reason` 里写清是哪一种，**不允许把"没核实"伪装成"符合预期"**；
- 失败 → failed / error。严格区分「请求已发出」与「动作已完成」，
  抑制大模型幻觉误动作（三段式状态设计参考 UAgentOS ActionResult，Apache-2.0）。
- 回读参数（传播等待 / 数值容差 / 不可信状态清单）走 config/trust.json → readback，
  真实适配器与虚拟设备共用同一套证据形状。
- 孪生同步：若设备存在孪生对（可执行数字孪生），回读结果同步写入实体侧
  占位适配器，形成「虚拟预期态 ↔ 实体实测态」一致性证据。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from ..config_loader import load_trust
from .schema import Device
from .virtual_devices import build_default_devices, apply_command

# 回读参数缓存：配置热重载要生效，但绝不能每条命令都读一次 JSON ——
# D27 的教训就是「规则层 98% 的延迟是一处磁盘 I/O」。这里按 10s TTL 缓存，
# 且 config/reload 时可通过 invalidate_readback_settings() 立即失效。
_SETTINGS_CACHE: dict = {"at": 0.0, "value": None}
_SETTINGS_TTL = 10.0


def readback_settings() -> dict:
    """回读参数（trust.json → readback），带 TTL 缓存。"""
    now = time.monotonic()
    if _SETTINGS_CACHE["value"] is not None and now - _SETTINGS_CACHE["at"] < _SETTINGS_TTL:
        return _SETTINGS_CACHE["value"]
    try:
        rb = (load_trust() or {}).get("readback") or {}
    except Exception:
        rb = {}
    value = {
        "settle_ms": float(rb.get("settle_ms", 0) or 0),
        "tolerance": float(rb.get("numeric_tolerance", 0.5) or 0),
        # 归一化成小写字符串集合：回读值可能来自真实适配器的字符串枚举
        "untrusted": {str(s).strip().lower() for s in (rb.get("untrusted_states") or [])},
    }
    _SETTINGS_CACHE.update({"at": now, "value": value})
    return value


def invalidate_readback_settings() -> None:
    """配置热重载后调用：让下一次回读重新读 trust.json。"""
    _SETTINGS_CACHE.update({"at": 0.0, "value": None})


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
            settings = readback_settings()
            if settings["settle_ms"] > 0:
                # 真实适配器（HA/Matter）的状态经状态机异步传播，紧贴调用读会拿到旧值。
                # 虚拟设备同步生效，所以 settle_ms 默认 0，不影响规则层延迟。
                time.sleep(settings["settle_ms"] / 1000.0)
            readback = self._readback(device, command, params, settings)
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
    def _readback(device: Device, command: str, params: dict,
                  settings: Optional[dict] = None) -> dict:
        """执行后回读设备属性，与目标值比对，生成**带原因**的证据。

        证据形状（向后兼容，只增字段）：
            {attribute, expected, actual, confirmed, trusted, reason, checked_at}

        为什么需要 `reason`：只有 confirmed 布尔值时，"没核实"与"核实了不符"
        在审计里长得一模一样。Aether 的做法是把三种结局分开回话
        （成功 / 已下发但状态不符 / 回读失败未经核实），这里把同一区分落到
        证据里，供审计、周报与前端展示直接引用。
        """
        settings = settings or readback_settings()
        attr_name = READBACK_ATTR.get(command)
        base = {"attribute": attr_name, "expected": None, "actual": None,
                "confirmed": False, "trusted": False, "reason": "",
                "checked_at": round(time.time(), 3)}
        if not attr_name:
            base["reason"] = f"命令 {command} 没有登记回读契约"
            return base
        if attr_name not in device.attributes:
            base["reason"] = f"设备无属性 {attr_name}，无法回读核实"
            return base

        actual = device.get(attr_name)
        base["actual"] = actual
        if _is_untrusted(actual, settings["untrusted"]):
            # unknown / unavailable / 空值不是"符合预期"，而是"这条证据不可用"。
            # 把 unknown 当成功，是回读校验最常见的假阳性。
            base["reason"] = f"回读值不可信（{actual!r}）"
            return base

        param_key = READBACK_PARAM.get(command)
        if param_key is None:
            # 目标值由命令语义决定：turn_on→True，open→100，close→0 等
            expected = _command_target_value(command)
        else:
            expected = params.get(param_key)
        base["expected"] = expected
        confirmed = _values_equal(actual, expected, settings["tolerance"])
        base["confirmed"] = confirmed
        base["trusted"] = True
        base["reason"] = ("回读一致" if confirmed
                          else f"回读不一致：期望 {expected!r}，实际 {actual!r}")
        return base

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
    """由命令语义确定回读目标值（on→布尔，position→数值，status→枚举字符串）。

    注意顺序：枚举语义的命令必须排在布尔分支之前，否则会被截胡
    （start_clean 曾被 turn_on 分支抢先匹配成 True，导致回读永久无法 confirmed）。
    """
    if command == "start_clean":
        return "cleaning"
    if command == "stop_clean":
        return "idle"
    if command in ("turn_on", "lock", "play_music", "start_stream"):
        return True
    if command in ("turn_off", "unlock", "stop_music", "stop_stream"):
        return False
    if command == "open":
        return 100
    if command == "close":
        return 0
    return None


def _values_equal(actual, expected, tolerance: float = 0.0) -> bool:
    """宽松数值比对（参数可能以 str 传入，属性为 int）。

    tolerance 只用于数值：浮点/色温这类属性经真实适配器往返会有精度损失。
    布尔语义不做数值宽松 —— 否则 "1" 会被当成 True，把字符串枚举混进开关判定。
    """
    if actual is None or expected is None:
        return actual is expected
    if actual == expected:
        return True
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    try:
        a, e = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return abs(a - e) <= max(0.0, float(tolerance or 0.0))


def _is_untrusted(actual, untrusted: set[str]) -> bool:
    """回读值是否属于「不可信」：None / 空串 / unknown / unavailable 等。"""
    if actual is None:
        return True
    if isinstance(actual, str):
        text = actual.strip().lower()
        return (not text) or text in untrusted
    return False
