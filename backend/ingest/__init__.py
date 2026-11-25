# -*- coding: utf-8 -*-
"""ESPectre 接入层（可选）。

这一层的输出只有**派生状态**：节点在线/新鲜度、motion/idle、score、RSSI。
原始 CSI 永不进入本层，也不落日志、不上云。
"""
from .espectre_mqtt import (
    SUBSCRIBE_KINDS,
    EspectreMQTTClient,
    available,
    parse_motion_payload,
    parse_topic,
)
from .node_registry import VALID_STATES, Node, NodeRegistry

__all__ = [
    "SUBSCRIBE_KINDS",
    "EspectreMQTTClient",
    "available",
    "parse_motion_payload",
    "parse_topic",
    "VALID_STATES",
    "Node",
    "NodeRegistry",
]
