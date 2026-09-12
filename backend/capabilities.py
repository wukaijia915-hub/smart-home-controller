# -*- coding: utf-8 -*-
"""能力注册表 + 虚实同构孪生配对（可执行数字孪生的底座）。

设计目标：
1. 能力契约与设备类别解耦：同一能力（如 lighting.set_brightness）由
   虚拟设备驱动与未来真实协议适配器共同实现——同一份契约驱动虚实两侧，
   孪生体与实体同状态、同动作、同验证。
2. 中文口语别名归一：把「好热」「太亮」等口语归一到能力契约，
   屏蔽设备品牌/协议差异，支撑跨品类统一管控。

思想参考 UAgentOS 的能力路由设计（Apache-2.0），按灵犀架构重写：
JSON 配置 + 设备类别契约 + 孪生配对，结构与其 YAML+namespace 实现不同。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "capabilities.json"


@dataclass
class CapabilityDefinition:
    """能力契约：一个语义能力（如 climate.set_temperature）及其设备类别实现映射。"""
    name: str
    description: str = ""
    device_categories: list[str] = field(default_factory=list)
    command: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    aliases: list[dict] = field(default_factory=list)   # [{keyword, params?}]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "device_categories": self.device_categories,
            "command": self.command,
            "params": self.params,
            "aliases": self.aliases,
        }


@dataclass
class PhysicalTwinPlaceholder:
    """真实设备协议适配器占位（Matter / Home Assistant 接口预留）。

    零硬件阶段：只保留「实体侧状态」并支持回读比对；
    生产环境替换为真实协议适配器后，对外行为契约不变。
    """
    device_id: str
    name: str
    protocol: str = "matter"
    state: dict[str, Any] = field(default_factory=dict)

    def apply(self, attrs: dict[str, Any]) -> None:
        """实体侧状态写入（模拟协议下发）。"""
        self.state.update(attrs)

    def readback(self, expected: dict[str, Any]) -> dict:
        """实体侧回读：比对实测状态与预期状态，返回孪生一致性证据。"""
        checks = {}
        for key, value in expected.items():
            actual = self.state.get(key)
            checks[key] = {
                "expected": value,
                "actual": actual,
                "confirmed": _values_equal(actual, value),
            }
        return {
            "device_id": self.device_id,
            "protocol": self.protocol,
            "checks": checks,
            "confirmed": all(c["confirmed"] for c in checks.values()),
        }


class CapabilityRegistry:
    """能力注册表：加载能力清单、别名归一、口语解析、孪生配对。"""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or CONFIG_PATH
        self.capabilities: dict[str, CapabilityDefinition] = {}
        self.alias_map: dict[str, str] = {}       # 别名关键字 → 能力名
        self.twin_pairs: dict[str, PhysicalTwinPlaceholder] = {}   # 虚拟设备 id → 实体占位
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            self.capabilities = {}
            self.alias_map = {}
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            self.capabilities = {}
            self.alias_map = {}
            return
        self.capabilities = {}
        self.alias_map = {}
        for item in raw.get("capabilities", []):
            cap = CapabilityDefinition(
                name=item["name"],
                description=item.get("description", ""),
                device_categories=item.get("device_categories", []),
                command=item.get("command", ""),
                params=item.get("params", {}),
                aliases=item.get("aliases", []),
            )
            self.capabilities[cap.name] = cap
            for alias in cap.aliases:
                keyword = alias.get("keyword", "")
                if keyword:
                    self.alias_map[keyword] = cap.name

    # ---------- 别名归一 ----------
    def canonical(self, capability: str) -> str:
        return self.alias_map.get(capability, capability)

    def supports_category(self, capability: str, category: str) -> bool:
        cap = self.capabilities.get(self.canonical(capability))
        return bool(cap) and (not cap.device_categories or category in cap.device_categories)

    # ---------- 口语解析 ----------
    def resolve(self, text: str) -> Optional[dict]:
        """自然语言 → 能力契约。未命中返回 None。

        返回：{"capability", "command", "params", "alias_keyword", "description"}
        """
        if not text:
            return None
        # 长关键字优先，避免「热」截胡「太热了」
        for keyword in sorted(self.alias_map, key=len, reverse=True):
            if keyword in text:
                cap = self.capabilities[self.alias_map[keyword]]
                params = dict(cap.params)
                for alias in cap.aliases:
                    if alias.get("keyword") == keyword and alias.get("params"):
                        params.update(alias["params"])
                return {
                    "capability": cap.name,
                    "command": cap.command,
                    "params": params,
                    "alias_keyword": keyword,
                    "description": cap.description,
                }
        return None

    # ---------- 孪生配对 ----------
    def pair_twin(self, virtual_device_id: str, physical_id: str,
                  name: str = "", protocol: str = "matter") -> PhysicalTwinPlaceholder:
        """建立孪生对：虚拟驱动（孪生体预期状态源）↔ 实体适配器（实测状态源）。"""
        twin = PhysicalTwinPlaceholder(physical_id, name or physical_id, protocol=protocol)
        self.twin_pairs[virtual_device_id] = twin
        return twin

    def get_twin(self, virtual_device_id: str) -> Optional[PhysicalTwinPlaceholder]:
        return self.twin_pairs.get(virtual_device_id)

    # ---------- 展示 ----------
    def list_capabilities(self) -> dict:
        return {
            "capabilities": [c.to_dict() for c in sorted(self.capabilities.values(), key=lambda x: x.name)],
            "aliases": dict(sorted(self.alias_map.items())),
            "twin_pairs": {
                v: {"physical_id": t.device_id, "protocol": t.protocol, "state": t.state}
                for v, t in self.twin_pairs.items()
            },
        }

    # ---------- 设备类别解析（能力 → 具体设备） ----------
    def find_devices(self, capability: str, registry) -> list:
        """按能力契约筛选设备：优先同房间，其次按类别匹配。"""
        cap = self.capabilities.get(self.canonical(capability))
        if not cap:
            return []
        candidates = []
        for device in registry.all():
            if device.state != "online":
                continue
            if cap.device_categories and device.category not in cap.device_categories:
                continue
            if cap.command and cap.command not in device.commands:
                continue
            candidates.append(device)
        # 孪生配对中的虚拟设备优先（孪生体是交互入口）
        candidates.sort(key=lambda d: 0 if d.device_id in self.twin_pairs else 1)
        return candidates


def _values_equal(actual: Any, expected: Any) -> bool:
    """宽松数值比对（参数可能以 str 传入，属性为 int）。"""
    if actual == expected:
        return True
    try:
        return float(actual) == float(expected)
    except (TypeError, ValueError):
        return False
