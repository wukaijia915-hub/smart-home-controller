#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESPectre 独立探针（P0）：只采集、不判断、不碰主链。

为什么单独一个脚本：
- P0 只回答一个问题——"这两个房间的 CSI 能不能长期稳定工作"；
- 它不能依赖 `backend/`，否则主链一改就把基线数据弄脏；
- 输出 JSONL，一行一条原始事件 + 后端接收时间，事后可复算。

用法（MQTT，推荐）：
    python tools/csi_probe.py --transport mqtt --host 127.0.0.1 --port 1883 \
        --out experiments/csi_baseline_20261120.jsonl

用法（HTTP SSE，备用；需要指定节点名）：
    python tools/csi_probe.py --transport http --http http://192.168.1.50:62587 \
        --node node-bedroom-01 --out experiments/csi_baseline_20261120.jsonl

依赖：MQTT 需要 `pip install paho-mqtt`；HTTP 只用标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

try:  # Windows 控制台默认 GBK，中文/符号会炸
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


class Writer:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.count = 0

    def write(self, record: dict):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.count += 1


def infer_kind(payload) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    if "state" in payload and "score" in payload:
        return "motion"
    if "rssi_dbm" in payload:
        return "wifi"
    if "status" in payload or "online" in payload:
        return "health"
    if "ready" in payload or "mode" in payload:
        return "sensing"
    return "unknown"


def run_mqtt(args, writer: Writer) -> int:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("✗ 未安装 paho-mqtt。请先：pip install -r requirements-csi.txt", file=sys.stderr)
        return 2

    prefix = args.prefix.rstrip("/")
    stats = {"motion": 0, "health": 0, "wifi": 0, "sensing": 0,
             "capabilities": 0, "fault": 0, "unknown": 0, "parse_error": 0}
    started = time.time()
    state = {"connected": False}

    def on_connect(client, userdata, flags, rc, properties=None):
        ok = getattr(rc, "value", rc) == 0
        state["connected"] = ok
        if ok:
            client.subscribe(f"{prefix}/+/#", qos=0)
            print(f"✓ 已连接 {args.host}:{args.port}，订阅 {prefix}/+/#")
        else:
            print(f"✗ 连接被拒绝：rc={rc}", file=sys.stderr)

    def on_disconnect(client, userdata, *callback_args):
        state["connected"] = False
        print("… 连接断开，paho 会自动重连")

    def on_message(client, userdata, message):
        ts = time.time()
        topic = message.topic
        head = prefix + "/"
        if not topic.startswith(head):
            return
        rest = topic[len(head):].split("/")
        if len(rest) < 2:
            return
        node_id, kind = rest[0], rest[1]
        if args.node and node_id != args.node:
            return

        raw = message.payload
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = text
            stats["parse_error"] += 1

        stats[kind] = stats.get(kind, 0) + 1
        writer.write({
            "received_at": ts,
            "received_iso": now_iso(ts),
            "source": "mqtt",
            "node_id": node_id,
            "kind": kind,
            "device_ts_ms": payload.get("timestamp_ms") if isinstance(payload, dict) else None,
            "payload": payload,
        })
        if writer.count % 100 == 0:
            print(f"… 已写入 {writer.count} 条  {stats}")

    try:
        client = mqtt.Client(client_id=f"xijian-probe-{int(time.time())}")
    except AttributeError:
        client = mqtt.Client()
    username = os.getenv(args.user_env, "")
    password = os.getenv(args.pass_env, "")
    if username:
        client.username_pw_set(username, password or None)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    try:
        client.connect(args.host, args.port, keepalive=30)
    except Exception as exc:
        print(f"✗ 连不上 broker {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1

    client.loop_start()
    print(f"开始采集 → {writer.path}（Ctrl+C 结束，--duration={args.duration}）")
    try:
        while True:
            if args.duration and (time.time() - started) >= args.duration:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n… 收到 Ctrl+C")
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass

    print(f"✓ 结束：{writer.count} 条 → {writer.path}")
    print(f"  分类：{stats}")
    return 0


def run_http(args, writer: Writer) -> int:
    if not args.http:
        print("✗ --transport http 需要 --http", file=sys.stderr)
        return 2
    if not args.node:
        print("✗ HTTP 模式必须指定 --node（一台设备一个流）", file=sys.stderr)
        return 2

    url = args.http.rstrip("/") + "/espectre/v1/events"
    stats = {"motion": 0, "health": 0, "wifi": 0, "sensing": 0,
             "capabilities": 0, "fault": 0, "unknown": 0, "parse_error": 0}
    started = time.time()
    print(f"开始采集（SSE）{url} → {writer.path}（Ctrl+C 结束）")

    try:
        while True:
            if args.duration and (time.time() - started) >= args.duration:
                break
            try:
                request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(request, timeout=35) as response:
                    event_name = None
                    for raw_line in response:
                        line = raw_line.decode("utf-8", "replace").strip()
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        try:
                            payload = json.loads(data)
                        except (TypeError, ValueError):
                            payload = data
                            stats["parse_error"] += 1
                        kind = event_name or infer_kind(payload)
                        event_name = None
                        ts = time.time()
                        stats[kind] = stats.get(kind, 0) + 1
                        writer.write({
                            "received_at": ts,
                            "received_iso": now_iso(ts),
                            "source": "http",
                            "node_id": args.node,
                            "kind": kind,
                            "device_ts_ms": payload.get("timestamp_ms") if isinstance(payload, dict) else None,
                            "payload": payload,
                        })
                        if writer.count % 100 == 0:
                            print(f"… 已写入 {writer.count} 条  {stats}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"… SSE 断开（{exc}），3 秒后重连", file=sys.stderr)
                time.sleep(3)
    except KeyboardInterrupt:
        print("\n… 收到 Ctrl+C")

    print(f"✓ 结束：{writer.count} 条 → {writer.path}")
    print(f"  分类：{stats}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ESPectre 独立探针（P0 基线采集）")
    parser.add_argument("--transport", choices=("mqtt", "http"), default="mqtt")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--prefix", default="espectre/v1/devices")
    parser.add_argument("--user-env", default="ESPECTRE_MQTT_USER")
    parser.add_argument("--pass-env", default="ESPECTRE_MQTT_PASSWORD")
    parser.add_argument("--http", default="", help="例：http://192.168.1.50:62587")
    parser.add_argument("--node", default="", help="只采这个节点；HTTP 模式必填")
    parser.add_argument("--out", default="", help="JSONL 输出路径")
    parser.add_argument("--duration", type=int, default=0, help="采集秒数；0 表示一直采到 Ctrl+C")
    args = parser.parse_args()

    out = args.out or os.path.join(
        "experiments", f"csi_baseline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
    writer = Writer(out)

    if args.transport == "mqtt":
        return run_mqtt(args, writer)
    return run_http(args, writer)


if __name__ == "__main__":
    raise SystemExit(main())
