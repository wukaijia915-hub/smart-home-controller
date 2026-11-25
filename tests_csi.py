# -*- coding: utf-8 -*-
"""CSI 接入与房间四态测试（P1a，无硬件可跑）。

这一组测试守的不是"功能有没有"，而是三条**语义**：

1. `idle` 只是"没有运动"，不是"房间无人"；
2. 节点掉线 / 事件流断了，只能 `degraded`，绝不推断无人；
3. 感知/接入层不得执行设备动作，也不得把原始 CSI 写进任何对外结构。
"""
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from backend.config_loader import load_adaptation, load_room_fusion, load_trust, load_wifi_csi
from backend.ingest import (
    EspectreMQTTClient,
    NodeRegistry,
    available as mqtt_available,
    parse_motion_payload,
    parse_topic,
)
from backend.perception.rooms import (
    DEGRADED,
    EMPTY_CONFIDENT,
    PRESENT_CONFIDENT,
    UNCERTAIN,
    RoomState,
    RoomTimeline,
    fuse_rooms,
)

PASSED, FAILED = 0, 0
T0 = 1_760_000_000.0


def check(label: str, condition: bool, detail: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {label}")
    else:
        FAILED += 1
        print(f"  ✗ {label}  {detail}")


def entry(state="idle", stale=False, idle_seconds=None, last_motion_at=None, online=True):
    return {
        "state": state,
        "stale": stale,
        "online": online,
        "idle_seconds": idle_seconds,
        "last_motion_at": last_motion_at,
    }


def one_room_config(threshold=300, degraded_on_stale=True, motion_overrides=True):
    return {
        "rooms": [{"room_id": "r", "label": "测试房间", "nodes": ["a", "b"], "privacy": "no_camera"}],
        "rules": {
            "empty_requires_all_nodes_idle_seconds": threshold,
            "empty_requires_min_corroborating_sources": 2,
            "degraded_on_node_stale": degraded_on_stale,
            "motion_overrides_idle": motion_overrides,
            "uncertain_on_conflict": True,
        },
        "confidence_weights": {"node_online": 0.3, "motion_recency": 0.4, "corroboration": 0.3},
        "motion_recency_window_seconds": 120,
    }


print("=" * 60)
print("  犀鉴 — CSI 接入与房间四态测试（P1a）")
print("=" * 60)

# ---------------------------------------------------------------- 1. 配置
print("\n== 1. 配置契约 ==")

wifi = load_wifi_csi()
rooms_cfg = load_room_fusion()
trust = load_trust()
adaptation = load_adaptation()

check("wifi_csi 已加载且默认 enabled=false", wifi.get("enabled") is False, str(wifi.get("enabled")))
check("wifi_csi 有 3 个节点", len(wifi.get("nodes") or []) == 3, str(len(wifi.get("nodes") or [])))
check("room_fusion 有 3 个房间", len(rooms_cfg.get("rooms") or []) == 3)
check("房间引用的节点都在 wifi_csi 里",
      {n for room in rooms_cfg.get("rooms", []) for n in room.get("nodes", [])}
      <= {n["node_id"] for n in wifi.get("nodes", [])})
check("默认每房间 1 个节点", all(len(r.get("nodes", [])) == 1 for r in rooms_cfg.get("rooms", [])))
check("无人阈值默认 300 秒", rooms_cfg["rules"]["empty_requires_all_nodes_idle_seconds"] == 300)
check("旁证至少 2 路", rooms_cfg["rules"]["empty_requires_min_corroborating_sources"] >= 2)
check("trust 撤销窗口 600 秒", trust.get("undo_window_seconds") == 600)
check("set_temperature 可逆", "set_temperature" in trust.get("reversible_commands", []))
check("门锁/燃气/摄像头在高危清单里",
      {(x["category"]) for x in trust.get("irreversible", [])} >= {"lock", "gas_stove", "camera"})
check("adaptation 有当日预算", int(adaptation.get("daily_budget", 0)) > 0)
check("adaptation 四个属性都有 min_interval",
      all("min_interval_seconds" in v for v in adaptation.get("attributes", {}).values()))
check("安全设备排除清单非空", "lock" in adaptation.get("safe_device_exclusions", []))

# ---------------------------------------------------------------- 2. 主题与载荷契约
print("\n== 2. MQTT 主题与载荷契约 ==")

check("主题解析：正常",
      parse_topic("espectre/v1/devices/node-bedroom-01/motion") == ("node-bedroom-01", "motion"))
check("主题解析：前缀不符返回 None",
      parse_topic("other/v1/devices/node-bedroom-01/motion") is None)
check("主题解析：缺 kind 返回 None",
      parse_topic("espectre/v1/devices/node-bedroom-01") is None)

good = parse_motion_payload('{"timestamp_ms": 42000, "state": "idle", "score": 0.0123}')
check("motion 载荷：解析 idle/score/设备时间",
      good == {"state": "idle", "score": 0.0123, "device_ts_ms": 42000}, str(good))
check("motion 载荷：bytes 也接受",
      parse_motion_payload(b'{"state":"motion","score":0.5}')["state"] == "motion")
check("motion 载荷：坏 JSON 返回 None", parse_motion_payload("{not json") is None)
check("motion 载荷：未知状态返回 None",
      parse_motion_payload('{"state":"present","score":0.1}') is None)
check("motion 载荷：缺 score 允许，置 None",
      parse_motion_payload('{"state":"idle"}')["score"] is None)
check("motion 载荷：字符串 score 可转数值",
      parse_motion_payload('{"state":"idle","score":"0.25"}')["score"] == 0.25)

# ---------------------------------------------------------------- 3. 节点注册表：两个时间戳
print("\n== 3. 节点注册表：在线 ≠ 数据新鲜 ==")

reg = NodeRegistry.from_config(wifi, now_fn=lambda: T0)
check("节点注册成功", reg.get("node-bedroom-01") is not None)
check("节点映射到房间", reg.get("node-bedroom-01").room_id == "bedroom")
check("初始节点 stale=True", reg.get("node-bedroom-01").stale is True)
check("未知节点写入返回 False", reg.update_motion("nope", "motion", received_at=T0) is False)
check("非法状态写入返回 False",
      reg.update_motion("node-bedroom-01", "present", received_at=T0) is False)

ok = reg.update_motion("node-bedroom-01", "motion", 0.5, 42000, received_at=T0)
node = reg.get("node-bedroom-01")
check("motion 事件被接受", ok is True)
check("motion 后 state=motion / stale=False", node.state == "motion" and node.stale is False)
check("motion 记录 last_motion_at", node.last_motion_at == T0)
check("motion 清空 idle_since", node.idle_since is None)
check("设备 timestamp_ms 只保存不用于跨节点比较", node.device_ts_ms == 42000)

reg.update_motion("node-bedroom-01", "idle", 0.1, 43000, received_at=T0 + 10)
first_idle = reg.get("node-bedroom-01").idle_since
reg.update_motion("node-bedroom-01", "idle", 0.1, 44000, received_at=T0 + 20)
check("首次 idle 设置 idle_since", first_idle == T0 + 10)
check("重复 idle 不重置 idle_since", reg.get("node-bedroom-01").idle_since == first_idle)

changed = reg.sweep(T0 + 55)
check("超过 30 秒无事件 → stale=True", reg.get("node-bedroom-01").stale is True, str(changed))
check("但 60 秒内仍有消息 → online=True", reg.get("node-bedroom-01").online is True)

# 最危险的一类：health 还活着，运动事件流已经断了
reg.update_liveness("node-bedroom-01", received_at=T0 + 60)
reg.sweep(T0 + 65)
node = reg.get("node-bedroom-01")
check("health 心跳只续 online，不给 stale 续命",
      node.online is True and node.stale is True,
      f"online={node.online}, stale={node.stale}")

never = reg.get("node-bathroom-01")
reg.sweep(T0 + 100)
check("从未上报的节点 stale=True / online=False",
      never.stale is True and never.online is False)

states = reg.states(T0 + 100)
check("states() 返回脱敏字段",
      set(states["node-bedroom-01"]) >= {"node_id", "room_id", "state", "stale", "online",
                                         "idle_seconds", "last_motion_at"})
check("states() 不含原始 CSI 字段",
      not any("csi" in k.lower() or "raw" in k.lower() for k in states["node-bedroom-01"]))
check("status() 统计字段齐全",
      set(reg.status(T0 + 100)) >= {"node_count", "online_count", "stale_count", "nodes", "stats"})

# ---------------------------------------------------------------- 4. 房间融合规则
print("\n== 4. 房间四态：idle ≠ empty ==")

cfg = one_room_config()
fused = fuse_rooms({"a": entry("motion", idle_seconds=0, last_motion_at=T0),
                    "b": entry("idle", idle_seconds=30)}, cfg, now=T0 + 5)
room = fused["r"]
check("任一节点新鲜 motion → present_confident", room.state == PRESENT_CONFIDENT, room.state)
check("present 置信度在 [0,1]", 0.0 <= room.confidence <= 1.0, str(room.confidence))
check("present 证据含 motion_detected", room.evidence["reason"] == "motion_detected")
check("present 不产生 empty 候选", room.evidence["empty_candidate"] is False)

fused = fuse_rooms({"a": entry("idle", idle_seconds=100),
                    "b": entry("idle", idle_seconds=100)}, cfg, now=T0 + 100)
room = fused["r"]
check("全部 idle 但不足 300 秒 → uncertain", room.state == UNCERTAIN, room.state)
check("不足阈值时不是 empty 候选", room.evidence["empty_candidate"] is False)
check("uncertain 置信度低于 0.6", room.confidence < 0.6, str(room.confidence))

fused = fuse_rooms({"a": entry("idle", idle_seconds=300),
                    "b": entry("idle", idle_seconds=301)}, cfg, now=T0 + 301)
room = fused["r"]
check("全部 idle 超过 300 秒 → 仍是 uncertain", room.state == UNCERTAIN, room.state)
check("但置 empty_candidate=True", room.evidence["empty_candidate"] is True)
check("融合层绝不输出 empty_confident", room.state != EMPTY_CONFIDENT)
check("原因写明等待旁证",
      room.evidence["reason"] == "empty_candidate_pending_corroboration", room.evidence["reason"])

fused = fuse_rooms({"a": entry("idle", idle_seconds=400),
                    "b": entry("idle", stale=True)}, cfg, now=T0 + 400)
room = fused["r"]
check("一个节点掉线 → uncertain 而非无人", room.state == UNCERTAIN, room.state)
check("覆盖不全时不是 empty 候选", room.evidence["empty_candidate"] is False)
check("原因标注 partial_coverage", room.evidence["reason"] == "partial_coverage")

fused = fuse_rooms({"a": entry("idle", stale=True), "b": entry("idle", stale=True)},
                   cfg, now=T0 + 500)
room = fused["r"]
check("全部节点 stale → degraded", room.state == DEGRADED, room.state)
check("degraded 置信度为 0", room.confidence == 0.0)
check("degraded 明确禁止推断无人",
      "禁止推断无人" in room.evidence["note"], room.evidence["note"])

fused = fuse_rooms({"a": entry("motion", stale=True, last_motion_at=T0),
                    "b": entry("motion", stale=True, last_motion_at=T0)}, cfg, now=T0 + 500)
check("过期的 motion 不能判有人", fused["r"].state == DEGRADED, fused["r"].state)

fused = fuse_rooms({"a": entry("unknown"), "b": entry("idle", idle_seconds=100)},
                   cfg, now=T0 + 100)
check("未知状态 → uncertain", fused["r"].state == UNCERTAIN, fused["r"].state)
check("原因标注 unknown_state", fused["r"].evidence["reason"] == "unknown_state")

no_nodes_cfg = {"rooms": [{"room_id": "empty", "label": "空房", "nodes": []}],
                "rules": {}, "confidence_weights": {}}
check("房间没有节点 → degraded",
      fuse_rooms({}, no_nodes_cfg, now=T0)["empty"].state == DEGRADED)
check("无硬件时 fuse_rooms 不抛异常",
      fuse_rooms({}, load_room_fusion(), now=T0)["bedroom"].state == DEGRADED)

# 配置驱动：阈值改小
small = one_room_config(threshold=60)
room = fuse_rooms({"a": entry("idle", idle_seconds=61),
                   "b": entry("idle", idle_seconds=61)}, small, now=T0 + 61)["r"]
check("阈值走配置（60 秒）", room.evidence["empty_candidate"] is True)

# 配置驱动：关闭 degraded
soft = one_room_config(degraded_on_stale=False)
room = fuse_rooms({"a": entry("idle", stale=True),
                   "b": entry("idle", stale=True)}, soft, now=T0)["r"]
check("关闭 degraded_on_node_stale → uncertain",
      room.state == UNCERTAIN and room.evidence["reason"] == "all_nodes_stale")

# 配置驱动：关闭 motion 优先
conflict = one_room_config(motion_overrides=False)
room = fuse_rooms({"a": entry("motion", last_motion_at=T0),
                   "b": entry("idle", idle_seconds=10)}, conflict, now=T0 + 5)["r"]
check("关闭 motion_overrides_idle → 冲突判 uncertain",
      room.state == UNCERTAIN and room.evidence["reason"] == "motion_idle_conflict")

# 可复现
fused1 = fuse_rooms({"a": entry("idle", idle_seconds=300),
                     "b": entry("idle", idle_seconds=300)}, cfg, now=T0 + 300)["r"].to_dict()
fused2 = fuse_rooms({"a": entry("idle", idle_seconds=300),
                     "b": entry("idle", idle_seconds=300)}, cfg, now=T0 + 300)["r"].to_dict()
check("同输入同 now → 结果完全一致", fused1 == fused2)

# ---------------------------------------------------------------- 5. 隐私与时间线
print("\n== 5. 隐私边界与时间线 ==")

room_dict = fused["r"].to_dict()
check("房间视图含四态与置信度",
      {"room_id", "state", "confidence", "evidence", "updated_at"} <= set(room_dict))
blob = json.dumps(room_dict, ensure_ascii=False).lower()
check("房间视图不含 csi/raw/画面/音频",
      not any(k in blob for k in ("raw_csi", "csi_data", "camera_frame", "audio_frame")))
check("房间视图证据不含跨节点 score 比较",
      "score" not in json.dumps(room_dict.get("evidence", {}), ensure_ascii=False).lower())

timeline = RoomTimeline(max_entries=5)
r1 = RoomState("r", "测试", PRESENT_CONFIDENT, 0.9, ["a"], T0, {"reason": "motion_detected"}, T0)
r2 = RoomState("r", "测试", PRESENT_CONFIDENT, 0.8, ["a"], T0, {"reason": "motion_detected"}, T0 + 1)
r3 = RoomState("r", "测试", UNCERTAIN, 0.4, ["a"], T0, {"reason": "idle_below_threshold"}, T0 + 2)
check("时间线首次追加成功", timeline.append(r1) is True)
check("状态未变不重复追加", timeline.append(r2) is False)
check("状态变化追加成功", timeline.append(r3) is True)
check("时间线只保留状态变化", len(timeline) == 2, str(len(timeline)))
check("时间线按房间过滤", timeline.recent(room_id="other") == [])
check("时间线 limit 生效", len(timeline.recent(limit=1)) == 1)
check("时间线条目含原因", timeline.recent(limit=1)[0]["reason"] == "idle_below_threshold")

# ---------------------------------------------------------------- 6. MQTT 客户端（无 broker）
print("\n== 6. MQTT 客户端：可选依赖与消息处理 ==")

check("available() 返回布尔", isinstance(mqtt_available(), bool))
client = EspectreMQTTClient({"enabled": False}, NodeRegistry.from_config(wifi))
check("enabled=false 时 start() 返回 False", client.start() is False)
check("未启动时 last_error 说明原因", bool(client.status()["last_error"]))
check("status() 字段齐全",
      {"available", "enabled", "started", "connected", "stats", "last_error"} <= set(client.status()))

handler_reg = NodeRegistry.from_config(wifi)
handler = EspectreMQTTClient({"mqtt": {"topic_prefix": "espectre/v1/devices"}}, handler_reg)
rec = handler._handle_message(
    "espectre/v1/devices/node-bedroom-01/motion",
    '{"timestamp_ms": 42000, "state": "motion", "score": 0.2}', received_at=T0)
check("motion 消息写入节点", rec is not None and rec["kind"] == "motion")
check("写入后节点 state=motion", handler_reg.get("node-bedroom-01").state == "motion")
check("主题前缀不符 → 返回 None",
      handler._handle_message("other/node/motion", "{}", received_at=T0) is None)
check("坏载荷计入 parse_errors",
      handler._handle_message("espectre/v1/devices/node-bedroom-01/motion", "{bad",
                              received_at=T0) is None
      and handler.status()["stats"]["parse_errors"] >= 1)

# 集成语义：health 来了，但 motion 事件停了 → 仍然 stale
handler._handle_message("espectre/v1/devices/node-bedroom-01/health",
                        '{"status": "ok", "online": true}', received_at=T0 + 40)
handler_reg.sweep(T0 + 45)
node = handler_reg.get("node-bedroom-01")
check("health 不刷新事件新鲜度（stale 仍为 True）", node.stale is True)
check("health 刷新在线状态（online=True）", node.online is True)
check("房间因事件过期转 degraded",
      fuse_rooms(handler_reg.states(T0 + 45), load_room_fusion(), now=T0 + 45)["bedroom"].state
      == DEGRADED)

# ---------------------------------------------------------------- 7. 静态扫描：红线
print("\n== 7. 静态扫描：接入/房间层不得执行动作 ==")

FORBIDDEN_CALLS = {"control"}
SCAN_FILES = []
for root in ("backend/ingest",):
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".py"):
                SCAN_FILES.append(os.path.join(dirpath, name))
SCAN_FILES.append(os.path.join("backend", "perception", "rooms.py"))

violations = []
for path in SCAN_FILES:
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_CALLS:
                violations.append(f"{path}:{node.lineno} 调用了 .{node.func.attr}(...)")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "issue" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "Lease":
            violations.append(f"{path}:{node.lineno} 调用了 Lease.issue")
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(m in node.module for m in ("devices.registry", "devices.schema")):
                violations.append(f"{path}:{node.lineno} 导入了设备层 {node.module}")
            if node.module.endswith("lease") or node.module == "backend.lease":
                violations.append(f"{path}:{node.lineno} 导入了租约层")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("lease") or alias.name.endswith("devices.registry"):
                    violations.append(f"{path}:{node.lineno} 导入了 {alias.name}")

check("接入/房间层无设备动作调用", not violations, "; ".join(violations[:3]))

# ---------------------------------------------------------------- 8. 多源校验
print("\n== 8. 多源交叉校验：单源不得判无人 ==")
from backend.perception.corroboration import (CONFLICT_ENVIRONMENT, CONFLICT_HUMAN,
                                              CONFLICT_PET, corroborate)
from backend.perception.room_service import RoomService


def _room_state(state=UNCERTAIN, candidate=True, room_id="bedroom"):
    return RoomState(room_id=room_id, label="卧室", state=state, confidence=0.3,
                     evidence={"empty_candidate": candidate, "reason": "test"})


_cfg = {"corroboration": {"min_sources": 2, "routine": {},
                          "power_active_threshold_w": 20,
                          "pir_motion_window_seconds": 120,
                          "action_window_seconds": 300,
                          "door_window_seconds": 300}}
_empty_ev = {"pir": {"motion": False, "last_motion_at": None}, "power_draw": 0.0,
             "recent_actions": [], "door": {"open": False, "last_open_at": None}}
_r = corroborate(_room_state(), _empty_ev, _cfg, now=T0)
check("CSI idle + 4 路旁证一致 → empty_confident",
      _r.room.state == EMPTY_CONFIDENT, _r.reason)
check("证据逐项标注 source_kind=simulated",
      all(s.source_kind == "simulated" for s in _r.sources))

_r1 = corroborate(_room_state(), {"pir": {"motion": False}}, _cfg, now=T0)
check("单源不足以判无人（需 >=2 路）", _r1.room.state == UNCERTAIN, _r1.reason)

_r2 = corroborate(_room_state(), {"pir": {"motion": True, "last_motion_at": T0},
                                  "power_draw": 60.0, "recent_actions": [],
                                  "door": {"open": False}}, _cfg, now=T0)
check("PIR 动作 + 高功率 → uncertain（human）",
      _r2.room.state == UNCERTAIN and _r2.conflict == CONFLICT_HUMAN, _r2.conflict)

_r3 = corroborate(_room_state(), {"pir": {"motion": False}, "power_draw": 80.0,
                                  "recent_actions": [], "door": {"open": False}},
                  _cfg, now=T0)
check("仅插座功率高 → environment（电器不是人）",
      _r3.room.state == UNCERTAIN and _r3.conflict == CONFLICT_ENVIRONMENT, _r3.conflict)

_r4 = corroborate(_room_state(), {"pir": {"motion": True, "last_motion_at": T0},
                                  "power_draw": 0.0, "recent_actions": [],
                                  "door": {"open": False}}, _cfg, now=T0)
check("小幅动作 + 无用电/无操作 → pet", _r4.conflict == CONFLICT_PET, _r4.conflict)

_r5 = corroborate(_room_state(), {"pir": {"motion": False}, "power_draw": 0.0,
                                  "recent_actions": [{"ts": T0 + 100}],
                                  "door": {"open": False}}, _cfg, now=T0)
check("未来时间戳的动作不计入窗口（回放/时钟偏移）",
      _r5.room.state == EMPTY_CONFIDENT, _r5.reason)

check("degraded 永不判无人",
      corroborate(_room_state(state=DEGRADED), _empty_ev, _cfg, T0).room.state == DEGRADED)
check("present 不被旁证改写",
      corroborate(_room_state(state=PRESENT_CONFIDENT), _empty_ev, _cfg, T0).room.state
      == PRESENT_CONFIDENT)

# ---------------------------------------------------------------- 9. 房间服务
print("\n== 9. 房间服务：阈值边界 / 状态迁移 / 降级 ==")
_wifi = {"enabled": False,
         "nodes": [{"node_id": "n1", "room_id": "bedroom", "label": "卧室"}],
         "stale_after_seconds": 600, "online_after_seconds": 600}
_room_cfg = {
    "rooms": [{"room_id": "bedroom", "label": "卧室", "nodes": ["n1"]}],
    "rules": {"empty_requires_all_nodes_idle_seconds": 300,
              "empty_requires_min_corroborating_sources": 2,
              "degraded_on_node_stale": True, "motion_overrides_idle": True,
              "uncertain_on_conflict": True},
    "confidence_weights": {"node_online": 0.3, "motion_recency": 0.4, "corroboration": 0.3},
    "motion_recency_window_seconds": 120,
    "corroboration": {"min_sources": 2, "routine": {}},
    "events": {"emit_on_transition": True, "present_event": "person_detected",
               "empty_event": "no_person", "degraded_event": "room_degraded",
               "cooldown_seconds": 0},
}
_sensors = {"pir_bedroom": False, "plug_power_bedroom": 1.0, "door_entry_open": False}
_svc = RoomService(None, store=None, sensors_getter=lambda: dict(_sensors),
                   wifi_config=_wifi, room_config=_room_cfg)
_svc.nodes.update_motion("n1", "motion", 0.2, None, received_at=T0)
_svc.tick(now=T0)
check("motion → present_confident", _svc.states["bedroom"].state == PRESENT_CONFIDENT)

_svc.nodes.update_motion("n1", "idle", 0.0, None, received_at=T0 + 1)
_svc.tick(now=T0 + 299)
check("idle 298s：仍 uncertain，未到 300s 阈值",
      _svc.states["bedroom"].state == UNCERTAIN
      and _svc.states["bedroom"].evidence.get("empty_candidate") is False,
      str(_svc.states["bedroom"].evidence.get("idle_seconds")))

_tick = _svc.tick(now=T0 + 302)
check("idle 301s + 旁证一致 → empty_confident",
      _svc.states["bedroom"].state == EMPTY_CONFIDENT)
check("状态迁移产生 no_person 事件",
      any(e["type"] == "no_person" for e in _tick["events"]), str(_tick["events"]))
check("timeline 记录状态变化", len(_svc.timeline_recent("bedroom")) >= 2)

# 多房间交替追加：去重必须按房间，否则每轮都会记一条 uncertain
_tl = RoomTimeline()
_living = RoomState(room_id="living", label="客厅", state=UNCERTAIN, confidence=0.3,
                    updated_at=T0)
_bedroom = RoomState(room_id="bedroom", label="卧室", state=UNCERTAIN, confidence=0.3,
                     updated_at=T0)
_tl.append(_living); _tl.append(_bedroom); _tl.append(_living)
check("timeline 按房间去重（不被其它房间穿插灌满）", len(_tl) == 2, f"{len(_tl)} 条")

_svc.tick(now=T0 + 700)
check("节点掉线 → degraded（绝不推断无人）",
      _svc.states["bedroom"].state == DEGRADED)

# ---------------------------------------------------------------- 汇总
print()
print("=" * 60)
total = PASSED + FAILED
print(f"  结果: {PASSED} passed, {FAILED} failed  (共 {total} 项)")
print("=" * 60)

sys.exit(1 if FAILED else 0)
