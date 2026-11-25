# -*- coding: utf-8 -*-
"""按意图裁剪「给模型看的设备清单 / 可调用工具集」。

## 为什么需要这一层

工具与设备不是越多越好。对照项目 Aether（75k 行、17 个内置工具）的实际记录：

- 内置工具从 22 砍到 17（删掉通用的 `http_request`、`scene_create`），理由是
  「通用工具的宽泛描述会吃掉意图」；
- 它们的注释里有一句很硬的观察：**工具名本身就是泄漏源** —— 模型会从
  `automation_rule_confirm` / `revise` 的名字类推出 `create` 存在，然后编造
  一个不存在的实体去创建规则。

灵犀的工具注册表是 **17 台设备 × 各自命令 ≈ 64 个工具**（`GET /api/tools`），
MCP 客户端一次 `tools/list` 全量拿走。数量本身不是问题，问题是**同一轮里没必要
让模型看见与这句话无关的 60 个选项**。

## 设计原则（三条，都很重要）

1. **无信号不裁剪**。「把屋子氛围弄温馨一点」没有任何房间/类别关键词，
   此时裁剪等于人为制造盲区 —— 必须返回全量。裁剪只发生在**用户已经点明了
   某个房间或某类设备**的时候。
2. **高危能力默认不可见**。门锁/燃气灶/摄像头直播这些命令，只有用户这句话里
   显式出现高危关键词才出现在清单里。模型对不该做的事「无感知」，比让它看见
   再拒绝更可靠（且仍会被 Manager 的高危二次确认兜住）。
3. **裁剪结果可复核**。`select_devices` 返回的 meta 记录「裁剪前/后、命中了哪些
   房间与类别、为什么没裁剪」，会进决策链路（trace），答辩时可当场指着它说
   「这一轮模型只看见 3 台设备，不是 17 台」。
"""
from __future__ import annotations

import time
from typing import Iterable, Optional

from ..config_loader import load_tool_scope

# 配置读取缓存：裁剪发生在慢环（每轮一次），但 claim 校验与 trace 也会读它。
# 10s TTL 让热重载几乎立刻生效，同时避免每条决策都做一次磁盘 I/O
# （D27 的教训：规则层 98% 的延迟来自一处意外的文件读写）。
_CACHE: dict = {"at": 0.0, "value": None}
_TTL = 10.0

# 配置文件缺失时的最小兜底：宁可「不裁剪」，也不要因为配置读不到而让模型
# 看不见任何设备（那会把所有模糊指令都变成兜底回复）。
_FALLBACK = {
    "enabled": False,
    "max_devices": 10,
    "max_tools": 24,
    "room_keywords": {},
    "category_keywords": {},
    "high_risk_keywords": [],
    "high_risk_commands": [],
}


def _config(cfg: Optional[dict]) -> dict:
    if cfg is not None:
        return cfg
    now = time.monotonic()
    if _CACHE["value"] is not None and now - _CACHE["at"] < _TTL:
        return _CACHE["value"]
    try:
        loaded = load_tool_scope()
    except Exception:
        loaded = {}
    merged = dict(_FALLBACK)
    merged.update(loaded or {})
    _CACHE.update({"at": now, "value": merged})
    return merged


def invalidate_scope_cache() -> None:
    """配置热重载后调用，让下一次裁剪重新读 tool_scope.json。"""
    _CACHE.update({"at": 0.0, "value": None})


def detect_signal(text: str, cfg: Optional[dict] = None) -> dict:
    """从一句话里提取房间/类别信号与「是否显式提到高危动作」。"""
    cfg = _config(cfg)
    text = text or ""
    rooms = [room for room, kws in (cfg.get("room_keywords") or {}).items()
             if any(str(k) and str(k) in text for k in (kws or []))]
    categories = [cat for cat, kws in (cfg.get("category_keywords") or {}).items()
                  if any(str(k) and str(k) in text for k in (kws or []))]
    high_risk = any(str(k) and str(k) in text
                    for k in (cfg.get("high_risk_keywords") or []))
    return {"rooms": rooms, "categories": categories, "high_risk": high_risk}


def has_signal(signal: dict) -> bool:
    return bool(signal.get("rooms") or signal.get("categories"))


def select_devices(text: str, devices: Iterable, cfg: Optional[dict] = None
                   ) -> tuple[list, dict]:
    """按意图挑选设备，返回 (设备列表, 可复核的裁剪说明)。

    - 房间与类别同时出现 → 取交集（「卧室的灯」）；交集为空时退回并集，
      并如实标注（「卧室的电视」没有对应设备，不能因此让模型一台都看不见）。
    - 只出现房间 → 该房间全部设备；只出现类别 → 该类全部设备。
    - 都没有 → 全量返回，`narrowed=False`。
    """
    cfg = _config(cfg)
    all_devices = list(devices)
    signal = detect_signal(text, cfg)
    meta = {
        "enabled": bool(cfg.get("enabled", True)),
        "narrowed": False,
        "before": len(all_devices),
        "after": len(all_devices),
        "rooms": signal["rooms"],
        "categories": signal["categories"],
        "high_risk_explicit": signal["high_risk"],
        "reason": "",
    }

    if not meta["enabled"]:
        meta["reason"] = "配置关闭（tool_scope.enabled=false）"
        return all_devices, meta
    if not has_signal(signal):
        meta["reason"] = "无房间/类别信号，保留全量（模糊指令不该被人为制造盲区）"
        return all_devices, meta

    rooms, cats = set(signal["rooms"]), set(signal["categories"])
    max_devices = int(cfg.get("max_devices", 10) or 10)

    # 1) 设备名精确命中优先（"客厅吊灯" 不应该把客厅另一盏灯一起带出来）
    name_hits, seen = [], set()
    for d in all_devices:
        if getattr(d, "name", "") and d.name in text and d.device_id not in seen:
            seen.add(d.device_id)
            name_hits.append(d)
    if name_hits:
        truncated = len(name_hits) > max_devices
        if truncated:
            name_hits = name_hits[:max_devices]
        meta.update({"narrowed": True, "after": len(name_hits),
                     "reason": "设备名精确命中" + (f"；超出上限 {max_devices} 台已截断" if truncated else "")})
        return name_hits, meta

    picked, fallback_union = [], False
    for d in all_devices:
        room_hit = d.room in rooms if rooms else False
        cat_hit = d.category in cats if cats else False
        if rooms and cats:
            if room_hit and cat_hit:
                picked.append(d)
        elif room_hit or cat_hit:
            picked.append(d)

    if rooms and cats and not picked:
        # 交集为空：退回并集，并标明这不是精确匹配
        fallback_union = True
        for d in all_devices:
            if (d.room in rooms) or (d.category in cats):
                picked.append(d)

    # 去重（同一台设备可能同时满足多个条件）
    seen, unique = set(), []
    for d in picked:
        if d.device_id in seen:
            continue
        seen.add(d.device_id)
        unique.append(d)

    truncated = len(unique) > max_devices
    if truncated:
        unique = unique[:max_devices]

    if not unique:
        meta["reason"] = "匹配为空，退回全量（不能因为裁剪让模型看不见任何设备）"
        return all_devices, meta

    meta.update({
        "narrowed": True,
        "after": len(unique),
        "reason": ("房间∩类别交集为空，已退回并集" if fallback_union else "按房间/类别命中")
                  + (f"；超出上限 {max_devices} 台已截断" if truncated else ""),
    })
    return unique, meta


def select_tools(text: str, tools: Iterable, devices_by_id: dict,
                 cfg: Optional[dict] = None) -> tuple[list, dict]:
    """按意图裁剪工具集。设备类工具跟随设备裁剪，高危命令默认不可见。"""
    cfg = _config(cfg)
    tools = list(tools)
    high_risk_cmds = set(cfg.get("high_risk_commands") or [])
    picked, meta = select_devices(text, list(devices_by_id.values()), cfg)
    kept_ids = {d.device_id for d in picked}

    kept, hidden_high_risk = [], 0
    for t in tools:
        if getattr(t, "device_id", None):
            if kept_ids and t.device_id not in kept_ids:
                continue
            if t.command in high_risk_cmds and not meta["high_risk_explicit"]:
                hidden_high_risk += 1
                continue
        kept.append(t)

    max_tools = int(cfg.get("max_tools", 24) or 24)
    # 只在真的按意图裁剪时才套工具数上限：无信号的情况必须保留全量，
    # 否则"模糊指令"会被 cap 成前 24 个工具，等于人为制造盲区。
    if meta.get("narrowed") and len(kept) > max_tools:
        kept = kept[:max_tools]

    meta.update({
        "tools_before": len(tools),
        "tools_after": len(kept),
        "high_risk_hidden": hidden_high_risk,
    })
    return kept, meta


def describe_scope(meta: dict) -> str:
    """把裁剪说明压成一句可放进 trace 的中文。"""
    if not meta.get("narrowed"):
        return f"未裁剪（{meta.get('before', 0)} 台设备）：{meta.get('reason', '')}"
    rooms = "/".join(meta.get("rooms") or []) or "—"
    cats = "/".join(meta.get("categories") or []) or "—"
    return (f"按意图裁剪：{meta.get('before')}→{meta.get('after')} 台"
            f"（房间={rooms}，类别={cats}），高危可见={meta.get('high_risk_explicit')}")
