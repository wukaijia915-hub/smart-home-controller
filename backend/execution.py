# -*- coding: utf-8 -*-
"""唯一写入口：所有设备动作从这里出去。

## 为什么要有这个文件

设计书要求每条决策记录 `signal → inference → gate → lease → action → readback`
六段链。但改造前有 4 条写路径：`Manager`、`EventRouter`、`/api/control`、场景应用，
其中后几条要么没有租约、要么 `gate` 段是空的 —— 照现状展示出来的链会"缺一段"。

因此把 `registry.control(` 的调用**收口到本模块**：

    Manager（对话/工具/确认） ─┐
    EventRouter（事件联动）   ─┼→ execution.execute_action() → registry.control()
    撤销（source=undo）       ─┤        ↓
    L5 自治（source=adaptation）┘   审计 + 六段链 + 撤销记录

`tests_trust.py` 会用 AST 扫描：除本模块与 `devices/registry.py` 外，
任何模块出现 `registry.control(` 都失败。

## 四道闸在哪里

- 参数包络（min/max/enum）：`verify_params()`，对话路径也走它；
- 最大步长 / 最小间隔 / 当日预算：`check_guards()`，仅当调用方传入 `bounds`
  （L5 自治与测试会传）时启用 —— 用户显式指令不受"步长"限制，但受包络限制；
- 高危动作：`allow_high_risk=False` 时直接拒绝（`gate` 层）。
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from .lease import LeaseValidator
from .trust.undo import snapshot_attributes, undo_store

#: 失败归因的层（与申报书"失败可定位至具体一层"对应）
LAYERS = ("sensor", "inference", "gate", "lease", "device", "readback")


def new_decision_id() -> str:
    return f"dec_{uuid.uuid4().hex[:12]}"


def verify_params(device, command: str, params: dict) -> tuple[bool, str]:
    """参数包络校验：设备属性元数据（min/max/options）就是包络。

    参数名与属性名的映射用 `toolkit.schema_gen.PARAM_SOURCE` —— 与工具契约、
    Root 的 prompt 共用同一份定义，避免"prompt 教模型传 brightness、包络却看 power"。
    """
    from .toolkit.schema_gen import PARAM_SOURCE

    attr_by_param = dict(PARAM_SOURCE.get(command, []))
    if not attr_by_param:
        return True, "ok"
    for pname, aname in attr_by_param.items():
        if pname not in params:
            continue
        attr = device.attributes.get(aname)
        if attr is None:
            continue
        value = params[pname]
        unit = getattr(attr, "unit", "") or ""
        if attr.type == "enum" and attr.options and value not in attr.options:
            return False, (f"参数 {pname}={value!r} 不在「{device.name}」的可选值 "
                           f"{attr.options} 内")
        if attr.type == "number":
            try:
                num = float(value)
            except (TypeError, ValueError):
                return False, f"参数 {pname} 必须是数值，实际收到 {value!r}"
            if attr.min is not None and num < float(attr.min):
                return False, (f"参数 {pname}={value} 低于「{device.name}」的下限 "
                               f"{attr.min}{unit}")
            if attr.max is not None and num > float(attr.max):
                return False, (f"参数 {pname}={value} 超过「{device.name}」的上限 "
                               f"{attr.max}{unit}")
        if attr.type == "bool" and not isinstance(value, bool):
            if str(value).strip().lower() not in ("true", "false", "1", "0"):
                return False, f"参数 {pname} 必须是布尔值，实际收到 {value!r}"
    return True, "ok"


def _bounds_attrs(bounds: Optional[dict]) -> dict:
    if not isinstance(bounds, dict):
        return {}
    return bounds.get("attributes") or bounds


def check_guards(device, command: str, params: dict, bounds: dict,
                 store, now: float) -> tuple[bool, str]:
    """L5 四道闸中的后三道：最大步长 / 最小间隔 / 当日预算。

    包络（min/max）在第一道闸 `verify_params()` 里已经查过，这里不重复。
    """
    from .toolkit.schema_gen import PARAM_SOURCE

    spec_all = _bounds_attrs(bounds)
    budget = bounds.get("daily_budget") if isinstance(bounds, dict) else None
    if budget is not None and store is not None and store.actions_today(now) >= int(budget):
        return False, f"当日动作预算已用尽（{int(budget)} 次）"

    attr_by_param = dict(PARAM_SOURCE.get(command, []))
    min_interval = None
    for pname, aname in attr_by_param.items():
        if pname not in params:
            continue
        spec = spec_all.get(aname) or spec_all.get(pname) or {}
        if not isinstance(spec, dict):
            continue
        value = params[pname]
        current = device.get(aname)
        if spec.get("max_delta") is not None and isinstance(value, (int, float)) \
                and isinstance(current, (int, float)):
            if abs(float(value) - float(current)) > float(spec["max_delta"]):
                return False, (f"{pname} 单次变化 {abs(float(value) - float(current)):g} "
                               f"超过最大步长 {spec['max_delta']}")
        if spec.get("min_interval_seconds") is not None:
            min_interval = max(float(min_interval or 0), float(spec["min_interval_seconds"]))

    if min_interval and store is not None:
        elapsed = store.seconds_since_action(device.device_id, now)
        if elapsed is not None and elapsed < min_interval:
            return False, (f"距上次动作仅 {elapsed:.0f}s，未达到最小间隔 {min_interval:.0f}s")
    return True, "ok"


def _failed_layer(result: dict) -> str:
    if not result.get("ok"):
        return "device"
    if result.get("status") == "delivered":
        return "readback"
    return ""


def execute_action(device_id: str, command: str, params: Optional[dict] = None, *,
                   registry, store=None, source: str = "rule", lease=None,
                   decision_id: str = "", causal: Optional[dict] = None,
                   allow_high_risk: bool = False, bounds: Optional[dict] = None,
                   now: Optional[float] = None) -> dict:
    """执行一个动作，并写入审计 + 六段链 + 撤销记录。"""
    params = dict(params or {})
    now = float(now if now is not None else time.time())
    decision_id = decision_id or new_decision_id()
    action_id = uuid.uuid4().hex[:12]
    if store is not None and store.get_decision(decision_id) is None:
        store.open_decision(decision_id, source, causal)

    def reject(reason: str, layer: str = "gate") -> dict:
        result = {"ok": False, "status": "rejected", "error": reason,
                  "device_id": device_id, "command": command,
                  "decision_id": decision_id, "action_id": action_id,
                  "failed_layer": layer}
        if store is not None:
            store.append_stage(decision_id, "gate",
                               {"rejected": reason, "layer": layer})
            store.set_failed_layer(decision_id, layer)
        return result

    device = registry.get(device_id)
    if device is None:
        return reject(f"设备不存在：{device_id}", "gate")
    if getattr(device, "state", "online") in ("offline", "fault"):
        return reject(f"设备「{device.name}」当前 {device.state}，拒绝执行", "device")
    if not allow_high_risk and LeaseValidator.is_high_risk(device.category, command):
        desc = LeaseValidator.high_risk_desc(device.category, command)
        return reject(f"高危动作「{desc}」需人工二次确认，自动路径不得执行", "gate")
    ok, reason = verify_params(device, command, params)
    if not ok:
        return reject(reason, "gate")
    if bounds is not None and store is not None:
        ok, reason = check_guards(device, command, params, bounds, store, now)
        if not ok:
            return reject(reason, "gate")
    if lease is not None and LeaseValidator.is_expired(lease, now):
        return reject("租约已过期", "lease")

    before = snapshot_attributes(device)
    if store is not None:
        store.append_stage(decision_id, "lease",
                           lease.to_dict() if lease else {"scope": "plan-level",
                                                          "source": source})

    result = registry.control(device_id, command, params)
    after = snapshot_attributes(device)
    layer = _failed_layer(result)
    if store is not None:
        store.bump()
        store.record(lease, device_id, command, params, result,
                     version_after=store.version, decision_id=decision_id,
                     before=before, after=after, failed_layer=layer,
                     action_id=action_id, source=source)
        store.append_stage(decision_id, "action", {
            "action_id": action_id, "device_id": device_id, "command": command,
            "params": params, "before": before, "after": after,
        })
        store.append_stage(decision_id, "readback", {
            "status": result.get("status", ""), "readback": result.get("readback"),
            "failed_layer": layer,
        })
        if layer:
            store.set_failed_layer(decision_id, layer)
        if result.get("ok"):
            store.note_action(device_id, now)
            undo_store.register(action_id=action_id, decision_id=decision_id,
                                device=device, command=command, params=params,
                                before=before, after=after,
                                status=result.get("status", ""), source=source, ts=now)

    return {**result, "decision_id": decision_id, "action_id": action_id,
            "before": before, "after": after, "failed_layer": layer}


def execute_plan(actions: list[dict], *, registry, store=None, source: str = "rule",
                 causal: Optional[dict] = None, lease=None, allow_high_risk: bool = False,
                 bounds: Optional[dict] = None, now: Optional[float] = None,
                 decision_id: str = "") -> dict:
    """执行一组动作（同一 decision_id）。返回 {ok, decision_id, results}。"""
    decision_id = decision_id or new_decision_id()
    if store is not None and store.get_decision(decision_id) is None:
        store.open_decision(decision_id, source, causal)
    results = []
    for action in actions or []:
        result = execute_action(
            action.get("device_id", ""), action.get("command", ""),
            action.get("params") or {}, registry=registry, store=store,
            source=source, lease=lease, decision_id=decision_id,
            causal=None, allow_high_risk=allow_high_risk, bounds=bounds, now=now,
        )
        results.append(result)
    ok = bool(results) and all(r.get("ok") for r in results)
    if store is not None:
        store.append_stage(decision_id, "summary",
                           {"total": len(results),
                            "ok": sum(1 for r in results if r.get("ok")),
                            "ok_all": ok})
    return {"ok": ok, "decision_id": decision_id, "results": results}
