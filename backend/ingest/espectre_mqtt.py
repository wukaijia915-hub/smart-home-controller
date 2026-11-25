# -*- coding: utf-8 -*-
"""ESPectre MQTT 接入：只消费事件，不解析原始 CSI。

契约来自 `espectre-main/docs/API.md`（本地核验，A 级）：

    topic   espectre/v1/devices/{device_id}/motion
    payload {"timestamp_ms": 42000, "state": "idle"|"motion", "score": 0.0123}

三条必须写进代码的语义：

1. `idle` 表示"没有运动"，**不等于**"房间无人"；
2. `timestamp_ms` 是设备单调时钟，**跨节点不可比**，只做设备内排序；
3. 原始 CSI 不在 MQTT 上（只能走 HTTP `/espectre/v1/csi`），本项目永不采集。

paho-mqtt 是可选依赖：未安装时 `available()=False`，主链照常启动。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

from .node_registry import NodeRegistry

try:  # 可选依赖：没装也不让主链 import 失败
    import paho.mqtt.client as mqtt  # type: ignore

    _PAHO_AVAILABLE = True
except Exception:  # pragma: no cover - 取决于环境
    mqtt = None  # type: ignore
    _PAHO_AVAILABLE = False

#: 订阅四类资源 + 故障事件；`#` 会匹配 device_id 下的所有子主题
SUBSCRIBE_KINDS = ("motion", "health", "wifi", "sensing", "capabilities", "fault")


def available() -> bool:
    """paho-mqtt 是否可用。未安装时调用方应优雅降级。"""
    return _PAHO_AVAILABLE


def parse_topic(topic: str, prefix: str = "espectre/v1/devices") -> Optional[tuple[str, str]]:
    """把主题拆成 (device_id, kind)；不是本前缀的主题返回 None。"""
    if not topic or not prefix:
        return None
    head = prefix.rstrip("/") + "/"
    if not topic.startswith(head):
        return None
    rest = topic[len(head):].split("/")
    if len(rest) < 2 or not rest[0] or not rest[1]:
        return None
    return rest[0], rest[1]


def parse_motion_payload(raw) -> Optional[dict]:
    """解析 motion 事件；形状不对返回 None（不猜、不补默认值）。

    返回 `{"state", "score", "device_ts_ms"}`；`score`/`timestamp_ms` 缺失时对应为 None，
    但 `state` 必须是 `idle`/`motion` 之一。
    """
    payload = _decode(raw)
    if not isinstance(payload, dict):
        return None
    state = payload.get("state")
    if state not in ("idle", "motion"):
        return None
    score = payload.get("score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    ts = payload.get("timestamp_ms")
    try:
        device_ts_ms = int(ts) if ts is not None else None
    except (TypeError, ValueError):
        device_ts_ms = None
    return {"state": state, "score": score, "device_ts_ms": device_ts_ms}


class EspectreMQTTClient:
    """一个薄的 MQTT 消费端：解析 → 写 NodeRegistry。"""

    def __init__(self, config: Optional[dict], registry: NodeRegistry, on_event=None):
        self.config = config or {}
        mqtt_cfg = self.config.get("mqtt") or {}
        self.host = mqtt_cfg.get("host", "127.0.0.1")
        self.port = int(mqtt_cfg.get("port", 1883))
        self.prefix = mqtt_cfg.get("topic_prefix", "espectre/v1/devices")
        self.keepalive = int(mqtt_cfg.get("keepalive_seconds", 30))
        self.username_env = mqtt_cfg.get("username_env", "ESPECTRE_MQTT_USER")
        self.password_env = mqtt_cfg.get("password_env", "ESPECTRE_MQTT_PASSWORD")
        self.registry = registry
        self.on_event = on_event
        self._client = None
        self._started = False
        self._connected = False
        self._last_error = ""
        self._stats = {
            "messages": 0,
            "motion": 0,
            "health": 0,
            "wifi": 0,
            "sensing": 0,
            "capabilities": 0,
            "fault": 0,
            "parse_errors": 0,
            "unknown_topics": 0,
        }

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """启动后台订阅。失败只记录原因，不抛异常。"""
        if not self.config.get("enabled", False):
            self._last_error = "wifi_csi.enabled=false，未启动 MQTT"
            return False
        if not _PAHO_AVAILABLE:
            self._last_error = "paho-mqtt 未安装（pip install -r requirements-csi.txt）"
            return False

        try:
            client_id = f"xijian-{uuid.uuid4().hex[:8]}"
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
            except (AttributeError, TypeError):  # paho 1.x
                client = mqtt.Client(client_id=client_id)

            username = os.getenv(self.username_env, "")
            password = os.getenv(self.password_env, "")
            if username:
                client.username_pw_set(username, password or None)

            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.connect_async(self.host, self.port, keepalive=self.keepalive)
            client.loop_start()
        except Exception as exc:  # 端口/主机写错时不拖垮主链
            self._last_error = f"MQTT 启动失败：{exc}"
            return False

        self._client = client
        self._started = True
        self._last_error = ""
        return True

    def stop(self):
        client = self._client
        self._client = None
        self._started = False
        self._connected = False
        if client is None:
            return
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass

    # ---------- 回调 ----------
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        code = getattr(rc, "value", rc)
        self._connected = (code == 0)
        if not self._connected:
            self._last_error = f"MQTT 连接被拒绝：rc={rc}"
            return
        # 一次订阅全部子主题；未知 kind 在 _handle_message 里计数，不静默丢弃
        client.subscribe(f"{self.prefix.rstrip('/')}/+/#", qos=0)

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False

    def _on_message(self, client, userdata, message):
        try:
            payload = message.payload
            self._handle_message(message.topic, payload, received_at=time.time())
        except Exception as exc:  # 单条坏消息不能让订阅线程死掉
            self._stats["parse_errors"] += 1
            self._last_error = f"消息处理失败：{exc}"

    # ---------- 解析与写入 ----------
    def _handle_message(self, topic: str, raw, received_at: Optional[float] = None) -> Optional[dict]:
        """处理一条消息；返回写入的分类结果，失败返回 None。

        独立成方法是为了能在没有 broker 的测试里直接驱动。
        """
        received_at = float(received_at if received_at is not None else time.time())
        parsed = parse_topic(topic, self.prefix)
        if parsed is None:
            self._stats["unknown_topics"] += 1
            return None
        node_id, kind = parsed
        payload = _decode(raw)
        self._stats["messages"] += 1

        if kind == "motion":
            data = parse_motion_payload(raw)
            if data is None:
                self._stats["parse_errors"] += 1
                return None
            ok = self.registry.update_motion(
                node_id, data["state"], data["score"], data["device_ts_ms"], received_at=received_at)
            if not ok:
                self._stats["parse_errors"] += 1
                return None
            self._stats["motion"] += 1
            result = {"node_id": node_id, "kind": kind, "payload": data, "received_at": received_at}
        elif kind == "health":
            self.registry.update_liveness(node_id, received_at=received_at)
            if isinstance(payload, dict) and payload.get("calibration_state"):
                self.registry.update_sensing(node_id, payload.get("calibration_state"), received_at)
            self._stats["health"] += 1
            result = {"node_id": node_id, "kind": kind, "payload": payload, "received_at": received_at}
        elif kind == "wifi":
            rssi = (payload or {}).get("rssi_dbm") if isinstance(payload, dict) else None
            self.registry.update_wifi(node_id, rssi, received_at=received_at)
            self._stats["wifi"] += 1
            result = {"node_id": node_id, "kind": kind, "payload": payload, "received_at": received_at}
        elif kind == "sensing":
            calibration = None
            if isinstance(payload, dict):
                calibration = payload.get("calibration_state")
                if calibration is None and isinstance(payload.get("calibration"), dict):
                    calibration = payload["calibration"].get("state")
            self.registry.update_sensing(node_id, calibration, received_at=received_at)
            self._stats["sensing"] += 1
            result = {"node_id": node_id, "kind": kind, "payload": payload, "received_at": received_at}
        elif kind == "capabilities":
            self.registry.update_liveness(node_id, received_at=received_at)
            self._stats["capabilities"] += 1
            result = {"node_id": node_id, "kind": kind, "payload": payload, "received_at": received_at}
        elif kind == "fault":
            self.registry.update_liveness(node_id, received_at=received_at)
            self._stats["fault"] += 1
            result = {"node_id": node_id, "kind": kind, "payload": payload, "received_at": received_at}
        else:
            self._stats["unknown_topics"] += 1
            return None

        if self.on_event is not None:
            try:
                self.on_event(result)
            except Exception:
                pass
        return result

    # ---------- 状态 ----------
    def status(self) -> dict:
        return {
            "available": _PAHO_AVAILABLE,
            "enabled": bool(self.config.get("enabled", False)),
            "started": self._started,
            "connected": self._connected,
            "host": self.host,
            "port": self.port,
            "topic_prefix": self.prefix,
            "stats": dict(self._stats),
            "last_error": self._last_error,
        }


def _decode(raw):
    """bytes/str → dict；失败返回 None（调用方决定计为 parse_error）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    return None
