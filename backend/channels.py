"""多渠道接入层：统一消息总线，支持 HTTP API / WebSocket / Webhook / MQTT 模拟。
参考 openvela AI Agent 的多渠道设计，外部系统可通过任意渠道发送指令。
"""
from __future__ import annotations
import json, time, logging
from typing import Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger("channels")


@dataclass
class ChannelMessage:
    """统一消息格式。"""
    channel: str  # http / websocket / webhook / mqtt
    source: str  # 来源标识（用户ID/设备ID等）
    text: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class ChannelResponse:
    """统一响应格式。"""
    ok: bool
    reply: str = ""
    actions: list = field(default_factory=list)
    channel: str = ""
    timestamp: float = field(default_factory=time.time)


class ChannelManager:
    """渠道管理器：统一注册、路由和消息分发。"""
    def __init__(self):
        self._handlers: dict[str, Callable] = {}
        self._channel_stats: dict[str, dict] = {
            "http": {"received": 0, "sent": 0},
            "websocket": {"received": 0, "sent": 0},
            "webhook": {"received": 0, "sent": 0},
            "mqtt": {"received": 0, "sent": 0},
        }
        self._mqtt_subscribers: dict[str, list[Callable]] = {}  # MQTT 模拟：topic -> callbacks

    def register_handler(self, channel: str, handler: Callable):
        """注册渠道消息处理器。"""
        self._handlers[channel] = handler
        logger.info(f"注册渠道处理器: {channel}")

    async def dispatch(self, msg: ChannelMessage) -> ChannelResponse:
        """分发消息到对应渠道处理器。"""
        self._channel_stats[msg.channel]["received"] += 1
        handler = self._handlers.get(msg.channel)
        if not handler:
            return ChannelResponse(ok=False, reply=f"渠道 {msg.channel} 未注册处理器", channel=msg.channel)
        try:
            result = await handler(msg)
            self._channel_stats[msg.channel]["sent"] += 1
            return result
        except Exception as e:
            logger.error(f"渠道 {msg.channel} 处理错误: {e}")
            return ChannelResponse(ok=False, reply=f"处理错误: {e}", channel=msg.channel)

    def get_stats(self) -> dict:
        return {"channels": self._channel_stats, "total_received": sum(s["received"] for s in self._channel_stats.values())}

    # ---------- MQTT 模拟（内存 broker） ----------
    def mqtt_publish(self, topic: str, payload: dict):
        """模拟 MQTT 发布消息。"""
        self._channel_stats["mqtt"]["received"] += 1
        for callback in self._mqtt_subscribers.get(topic, []):
            try:
                callback(topic, payload)
            except Exception as e:
                logger.error(f"MQTT 订阅者错误: {e}")
        # 通配符订阅
        for sub_topic, callbacks in self._mqtt_subscribers.items():
            if sub_topic.endswith("#") and topic.startswith(sub_topic[:-1]):
                for callback in callbacks:
                    try:
                        callback(topic, payload)
                    except Exception:
                        pass

    def mqtt_subscribe(self, topic: str, callback: Callable):
        """模拟 MQTT 订阅。"""
        if topic not in self._mqtt_subscribers:
            self._mqtt_subscribers[topic] = []
        self._mqtt_subscribers[topic].append(callback)
        logger.info(f"MQTT 订阅: {topic}")


# 全局单例
channel_manager = ChannelManager()
