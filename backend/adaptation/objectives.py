# -*- coding: utf-8 -*-
"""目标定义与多设备确定性仲裁（L5 的"协商"就在这里）。

## 为什么需要仲裁

空调和除湿机都会改变体感；热水器和洗衣机争夺峰谷电价；扫地机与在场状态冲突。
如果每个设备各自按自己的规则调节，会出现"空调制冷、除湿机加热补偿"这种互相抵消。
本模块用一个统一的、**确定性的**目标函数裁决：舒适优先于节能、可逆优先于不可逆；
同分时按设备 id 排序，保证同一输入两次结果完全一致（可回放、可解释）。

大模型不参与仲裁 —— 它只负责理解自然语言与生成解释。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: 优先级：数字越小越优先
PRIORITY_COMFORT = 0
PRIORITY_ENERGY = 1

#: 属性 → 方向（用于发现"互相抵消"的提议）
_RAISE = {"humidifier": "up", "ac": "temp", "dehumidifier": "down",
          "water_heater": "heat", "fan": "air"}


@dataclass
class Proposal:
    proposal_id: str
    device_id: str
    device_name: str
    category: str
    room: str
    command: str
    params: dict = field(default_factory=dict)
    attribute: str = ""
    value: Optional[float] = None
    priority: int = PRIORITY_COMFORT
    reason: str = ""
    expected_comfort: float = 0.0     # 0~1，越大越舒适
    energy_cost: float = 0.0          # 相对值，越小越省
    gate_ok: bool = True
    gate_reason: str = ""
    executable: bool = False

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "device_id": self.device_id, "device_name": self.device_name,
            "category": self.category, "room": self.room,
            "command": self.command, "params": self.params,
            "attribute": self.attribute, "value": self.value,
            "priority": self.priority, "reason": self.reason,
            "expected_comfort": round(self.expected_comfort, 3),
            "energy_cost": round(self.energy_cost, 3),
            "gate_ok": self.gate_ok, "gate_reason": self.gate_reason,
            "executable": self.executable,
        }


def _direction(proposal: Proposal, context: dict) -> str:
    """这条提议会把属性往哪个方向推：up / down / hold。"""
    attr = proposal.attribute
    if attr == "temperature":
        mode = (proposal.params or {}).get("mode", "")
        if proposal.command == "turn_off":
            return "hold"
        if mode == "heat":
            return "up"
        if mode == "cool":
            return "down"
        # 只设温度：与当前目标比
        target = proposal.value
        current = ((context.get("sensors") or {}).get("temp_indoor"))
        if target is None or not isinstance(current, (int, float)):
            return "hold"
        return "down" if target < current else "up"
    if attr == "target":
        # 加湿/除湿的"方向"由设备类别决定，而不是由目标值与当前湿度比大小：
        # 除湿机目标 45 与加湿器目标 60，在 70% 湿度下都"低于当前"，但意图相反。
        if proposal.category == "humidifier":
            return "up"
        if proposal.category == "dehumidifier":
            return "down"
        current = ((context.get("sensors") or {}).get("humidity"))
        if proposal.value is None or not isinstance(current, (int, float)):
            return "hold"
        return "down" if proposal.value < current else "up"
    if attr == "humidity":
        return "down"
    if proposal.command == "turn_on" and proposal.category == "water_heater":
        return "heat"
    return "hold"


def arbitrate(proposals: list[Proposal], context: Optional[dict] = None,
              config: Optional[dict] = None) -> dict:
    """确定性仲裁：返回 {chosen, deferred, reason}。

    - 安全设备（门锁/燃气/摄像头/窗户）永不进入候选；
    - 同一属性上方向相反的两条提议，只保留一条（按当前读数决定谁更合适）；
    - 其余按（优先级，舒适增益，能耗，设备 id）排序。
    """
    cfg = config or {}
    context = context or {}
    excluded = set(cfg.get("safe_device_exclusions") or [])

    candidates = [p for p in (proposals or []) if p.category not in excluded]
    deferred: list[dict] = [
        {"proposal_id": p.proposal_id, "reason": "安全/门禁设备不参与 L5"}
        for p in (proposals or []) if p.category in excluded
    ]

    # 规范化排序，保证"输入顺序不同、结果相同"
    candidates.sort(key=lambda p: (p.attribute, p.category, p.device_id, p.command))

    # 同属性反向冲突：湿度（加湿 vs 除湿）、温度（制冷 vs 制热）
    by_attr: dict[str, list[Proposal]] = {}
    for p in candidates:
        by_attr.setdefault(p.attribute, []).append(p)

    kept: list[Proposal] = []
    for attr, group in by_attr.items():
        dirs = {_direction(p, context) for p in group}
        if {"up", "down"} <= dirs:
            # 有真实冲突：用当前读数决定保留哪一边
            humidity = ((context.get("sensors") or {}).get("humidity"))
            prefer_down = True
            if attr == "target" and isinstance(humidity, (int, float)):
                prefer_down = humidity > 55
            keep_dir = "down" if prefer_down else "up"
            for p in group:
                if _direction(p, context) == keep_dir:
                    kept.append(p)
                else:
                    deferred.append({"proposal_id": p.proposal_id,
                                     "reason": f"{attr} 方向冲突，按当前读数取 {keep_dir}"})
        else:
            kept.extend(group)

    kept.sort(key=lambda p: (p.priority, -p.expected_comfort, p.energy_cost,
                             p.device_id, p.command))
    return {
        "chosen": kept,
        "deferred": deferred,
        "reason": (f"候选 {len(candidates)} 条，保留 {len(kept)} 条；"
                   f"规则：舒适优先于节能 / 可逆优先于不可逆 / 同分按设备 id"),
    }
