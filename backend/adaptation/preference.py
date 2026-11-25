# -*- coding: utf-8 -*-
"""偏好带学习：按「能力属性 × 房间 × 时段」统计用户实际设定值的分位带。

## 为什么按属性而不是按设备

L5 要求"不按设备型号组织策略"：空调、除湿机、热水器都可以归到
`temperature` / `humidity` / `target` 这几类能力属性下。换一台同类别设备，
偏好带仍然适用；换一个家庭，偏好带必须重学（不跨家庭复制）。

## 冷启动

样本 < `cold_start_samples`（默认 12）时返回 `cold_start=True`，只给区间
不给"可执行建议"。这是有意的：3 个样本算出来的 p20/p80 不是偏好，是噪声。

## 数据来源

`StateStore.audit_log` 里成功的 set_* 动作（params 即用户设定值）。
不读设备、不执行动作 —— 本模块是纯函数，便于单测与回放。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..toolkit.schema_gen import PARAM_SOURCE

#: 命令 → 能力属性（用 PARAM_SOURCE 反查，避免另写一份映射）
_CMD_ATTR = {cmd: pairs[0][1] for cmd, pairs in PARAM_SOURCE.items() if pairs}
_LEARNABLE = {"temperature", "target", "brightness", "position", "volume", "color_temp"}


def hour_bucket(ts: float) -> str:
    """时段分桶：深夜 / 上午 / 下午 / 晚间。"""
    import time
    hour = time.localtime(float(ts)).tm_hour
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


@dataclass
class PreferenceBand:
    attribute: str
    room: str
    bucket: str
    low: float
    high: float
    center: float
    samples: int
    cold_start: bool = False
    source: str = "audit"

    def to_dict(self) -> dict:
        return {
            "attribute": self.attribute, "room": self.room, "bucket": self.bucket,
            "low": round(self.low, 2), "high": round(self.high, 2),
            "center": round(self.center, 2), "samples": self.samples,
            "cold_start": self.cold_start, "source": self.source,
        }

    def contains(self, value: float, margin: float = 0.0) -> bool:
        return (self.low - margin) <= float(value) <= (self.high + margin)


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(round((len(sorted_values) - 1) * q))
    return float(sorted_values[max(0, min(len(sorted_values) - 1, idx))])


def learn_bands(entries: Iterable[dict], config: Optional[dict] = None,
                registry=None) -> dict[tuple[str, str, str], PreferenceBand]:
    """从审计记录学偏好带，返回 {(attribute, room, bucket): Band}。"""
    cfg = config or {}
    min_samples = int(cfg.get("cold_start_samples", 12) or 12)
    groups: dict[tuple[str, str, str], list[float]] = {}

    for entry in entries or []:
        if not entry.get("ok"):
            continue
        command = str(entry.get("command") or "")
        attribute = _CMD_ATTR.get(command)
        if attribute not in _LEARNABLE:
            continue
        params = entry.get("params") or {}
        value = next((v for k, v in params.items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)), None)
        if value is None:
            continue
        room = ""
        device_id = str(entry.get("device_id") or "")
        if registry is not None:
            device = registry.get(device_id)
            room = getattr(device, "room", "") if device is not None else ""
        else:
            room = str(entry.get("room") or "")
        bucket = hour_bucket(float(entry.get("ts", 0) or 0))
        groups.setdefault((attribute, room, bucket), []).append(float(value))

    bands = {}
    for key, values in groups.items():
        values.sort()
        low = _quantile(values, 0.2)
        high = _quantile(values, 0.8)
        bands[key] = PreferenceBand(
            attribute=key[0], room=key[1], bucket=key[2],
            low=low, high=high, center=(low + high) / 2.0,
            samples=len(values), cold_start=len(values) < min_samples,
        )
    return bands


def best_band(bands: dict, attribute: str, room: str, bucket: str,
              ) -> Optional[PreferenceBand]:
    """优先精确 (属性, 房间, 时段)；退到 (属性, 房间, *) 与 (属性, *, 时段)。"""
    for key in ((attribute, room, bucket), (attribute, room, ""),
                (attribute, "", bucket), (attribute, "", "")):
        band = bands.get(key)
        if band is not None:
            return band
    return None
