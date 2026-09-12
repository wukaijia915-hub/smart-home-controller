# -*- coding: utf-8 -*-
"""智能家居数字人中枢 —— 后端入口。
FastAPI + WebSocket：REST 查询 + 实时指令/状态推送 + 前端静态托管。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .agent import HomeAgent
from .devices.registry import DeviceRegistry
from .scenes import SCENES
from .sensors import SensorSimulator

try:
    import edge_tts
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, message: dict):
        data = json.dumps(message, ensure_ascii=False)
        for ws in list(self.connections):
            try:
                await ws.send_text(data)
            except Exception:
                self.disconnect(ws)


registry = DeviceRegistry()
manager = ConnectionManager()
agent = HomeAgent(registry)
simulator = SensorSimulator(registry)

# 初始化标准化工具注册表
from .tools import tool_registry
tool_registry.build_from_devices(registry)

# ---- 能力注册表 + 事件路由引擎（可执行数字孪生 + 证据链闭环） ----
from .capabilities import CapabilityRegistry
from .event_routes import EventRouter

capability_registry = CapabilityRegistry()
event_router = EventRouter(registry, manager, state_store=None)


def _twin_sync(device, attrs: dict) -> dict:
    """孪生同步回调：虚拟设备属性 → 实体侧占位适配器 → 回读一致性证据。"""
    twin = capability_registry.get_twin(device.device_id)
    if twin is None:
        return None
    twin.apply(attrs)
    # 实体侧回读全部当前属性，比对虚拟预期态
    return twin.readback(attrs)


registry.set_twin_sync(_twin_sync)

_loop = None  # 主事件循环引用（跨线程广播用）


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()

    # 传感器变化 → 广播
    async def sensor_loop():
        while True:
            await asyncio.sleep(2.0)
            simulator.tick()
            agent.sensors = simulator.values  # 注入 Agent 环境感知
            await manager.broadcast({"type": "sensor", "data": simulator.values})

    # 主动告警：环境异常/设备故障时数字人主动提示
    last_alert = {}
    async def alert_loop():
        while True:
            await asyncio.sleep(5.0)
            s = simulator.values
            alerts = []
            # 温度异常
            if s.get("temp_indoor", 25) > 30:
                alerts.append(("temp_high", f"当前室内温度 {s['temp_indoor']:.0f}℃，已超过舒适阈值，建议开启空调降温。"))
            elif s.get("temp_indoor", 25) < 15:
                alerts.append(("temp_low", f"当前室内温度 {s['temp_indoor']:.0f}℃，偏冷，建议开启空调制热。"))
            # 空气质量差
            if s.get("air_quality", 85) < 50:
                alerts.append(("air_poor", f"当前空气质量指数 {s['air_quality']:.0f}，较差，建议开窗通风或开启新风。"))
            # 设备故障/离线
            for d in registry.all():
                if d.state in ("offline", "fault"):
                    key = f"device_{d.device_id}_{d.state}"
                    if key not in last_alert:
                        alerts.append((key, f"设备「{d.name}」当前状态为{d.state == 'offline' and '离线' or '故障'}，请检查。"))
                        last_alert[key] = True
                else:
                    key_off = f"device_{d.device_id}_offline"
                    key_fault = f"device_{d.device_id}_fault"
                    last_alert.pop(key_off, None)
                    last_alert.pop(key_fault, None)
            # 推送告警（去重：同一类型 60 秒内不重复）
            now = asyncio.get_event_loop().time()
            for key, msg in alerts:
                if key not in last_alert or now - last_alert[key] > 60:
                    last_alert[key] = now
                    await manager.broadcast({"type": "alert", "message": msg, "level": "warning"})

    # 设备变更 → 广播（可能来自 asyncio.to_thread 工作线程）
    def on_device_change(device, command, result):
        if _loop is not None:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "device_state", "device": device.to_dict()}), _loop
            )

    registry.on_change(on_device_change)

    # 初始化定时任务调度器
    from .scheduler import init_scheduler
    init_scheduler(app, registry, manager)

    task = asyncio.create_task(sensor_loop())
    alert_task = asyncio.create_task(alert_loop())
    yield
    task.cancel()
    alert_task.cancel()


app = FastAPI(title="智能家居数字人中枢", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- REST ----------------
@app.get("/api/devices")
async def api_devices():
    return {"devices": registry.snapshot()}


@app.get("/api/sensors")
async def api_sensors():
    return {"sensors": simulator.values}


@app.get("/api/scenes")
async def api_scenes():
    return {"scenes": {k: v["desc"] for k, v in SCENES.items()}}


@app.get("/api/config")
async def api_config():
    return {
        "devices": registry.snapshot(),
        "sensors": simulator.values,
        "scenes": {k: v["desc"] for k, v in SCENES.items()},
        "llm_configured": agent.llm.available,
    }


@app.get("/api/capabilities")
async def api_capabilities():
    """能力注册表：能力清单 + 别名表 + 孪生对（可执行数字孪生）。"""
    return capability_registry.list_capabilities()


@app.post("/api/events/route")
async def api_events_route(body: dict):
    """注入结构化事件（如端侧视觉检测结论）并路由为设备动作 + 告警。

    body: {"type": "fall_detected", "source": "edge_vision", "payload": {...}}
    返回 RouteEvaluation（匹配路由 / 动作结果含 confirmed/delivered 状态与回读证据）。
    """
    event = {
        "type": body.get("type", ""),
        "source": body.get("source", ""),
        "payload": body.get("payload") or {},
    }
    if not event["type"]:
        return JSONResponse({"ok": False, "error": "type 不能为空"}, status_code=400)
    evaluation = await asyncio.to_thread(event_router.route, event)
    return {"ok": True, **evaluation.to_dict()}


@app.post("/api/scene")
async def api_scene(body: dict):
    scene = body.get("scene", "")
    if scene not in SCENES:
        return JSONResponse({"ok": False, "error": f"未知场景: {scene}"}, status_code=400)
    results = [registry.control(a["device_id"], a["command"], a.get("params", {}))
               for a in SCENES[scene]["actions"]]
    await manager.broadcast({"type": "scene", "scene": scene, "desc": SCENES[scene]["desc"]})
    return {"ok": True, "results": results}


@app.post("/api/chat")
async def api_chat(body: dict):
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text 不能为空"}, status_code=400)
    result = await asyncio.to_thread(agent.decide, text)
    return {"ok": True, **result}


@app.post("/api/confirm")
async def api_confirm(body: dict):
    """高危操作二次确认：body {pending_id}，确认后执行挂起的动作。

    设备状态变更会经 registry.on_change 回调自动广播，此处无需手动广播。
    """
    pending_id = body.get("pending_id", "")
    if not pending_id:
        return JSONResponse({"ok": False, "error": "pending_id 不能为空"}, status_code=400)
    result = agent.confirm(pending_id)
    return {"ok": True, **result}


@app.post("/api/control")
async def api_control(body: dict):
    """开放接入 SDK 示例：外部直接控制设备。"""
    result = registry.control(body.get("device_id", ""), body.get("command", ""), body.get("params", {}))
    return result


@app.post("/api/edge")
async def api_edge(body: dict):
    """边缘场景：模拟设备离线/故障/恢复。"""
    device_id = body.get("device_id", "")
    state = body.get("state", "offline")
    registry.set_state(device_id, state)
    return {"ok": True, "device_id": device_id, "state": state}


@app.post("/api/config/reload")
async def api_config_reload():
    """JSON 配置热重载：重新加载设备/场景/环境配置，无需重启服务。"""
    from .config_loader import reload_all
    from .devices.virtual_devices import build_default_devices
    from .scenes import load_scenes
    cfg = reload_all()
    # 重新创建设备注册表（保留当前状态）
    new_devices = build_default_devices()
    registry.reload(new_devices)
    # 重新加载场景
    global SCENES
    SCENES = load_scenes()
    # 重新加载传感器配置
    simulator.config = cfg.get("environment", {})
    return {"ok": True, "devices": len(cfg.get("devices", [])), "scenes": len(cfg.get("scenes", {}))}


@app.get("/api/knowledge/stats")
async def api_knowledge_stats():
    """RAG 知识库统计：设备说明书数量、用户习惯记录数。"""
    from .knowledge.rag import kb
    return {"ok": True, **kb.get_stats()}


@app.get("/api/knowledge/context")
async def api_knowledge_context(text: str = ""):
    """RAG 知识检索：针对用户指令返回检索到的设备知识和用户习惯。"""
    from .knowledge.rag import kb
    context = kb.get_context(text) if text else ""
    manuals = kb.search_manuals(text, top_k=3) if text else []
    return {
        "ok": True,
        "query": text,
        "context": context,
        "manuals": [{"name": m["name"], "content": m["content"], "energy_tip": m.get("energy_tip", "")} for m in manuals],
        "habit_hint": kb.get_habit_hint(text),
    }


@app.get("/api/config/summary")
async def api_config_summary():
    """JSON 配置摘要：当前设备数、场景数、环境配置。"""
    from .config_loader import load_devices, load_scenes, load_environment
    devices = load_devices()
    scenes = load_scenes()
    env = load_environment()
    return {
        "ok": True,
        "devices_count": len(devices),
        "scenes_count": len(scenes),
        "devices": [{"id": d["device_id"], "name": d["name"], "category": d["category"], "room": d["room"], "protocol": d.get("protocol", "virtual")} for d in devices],
        "scenes": {k: v.get("desc", k) for k, v in scenes.items()},
        "environment": {k: v.get("initial", v) if isinstance(v, dict) else v for k, v in env.get("sensors", {}).items()},
    }


# ---------- 定时任务 ----------
@app.get("/api/schedule/tasks")
async def api_schedule_list():
    """列出所有定时任务。"""
    from .scheduler import list_tasks
    return {"ok": True, "tasks": list_tasks()}


@app.post("/api/schedule/tasks")
async def api_schedule_add(body: dict):
    """创建定时任务。body: {name, hour, minute, action_type: scene|command, scene|device_id+command}"""
    from .scheduler import add_task
    task_id = add_task(body)
    return {"ok": True, "id": task_id}


@app.delete("/api/schedule/tasks/{task_id}")
async def api_schedule_remove(task_id: str):
    """删除定时任务。"""
    from .scheduler import remove_task
    ok = remove_task(task_id)
    return {"ok": ok}


@app.post("/api/schedule/tasks/{task_id}/toggle")
async def api_schedule_toggle(task_id: str, body: dict):
    """启用/禁用定时任务。"""
    from .scheduler import toggle_task
    ok = toggle_task(task_id, body.get("enabled", True))
    return {"ok": ok}


# ---------- 标准化工具注册表 ----------
@app.get("/api/tools")
async def api_tools_list():
    """列出所有已注册工具（含 schema）。"""
    from .tools import tool_registry
    return {
        "ok": True,
        "total": len(tool_registry.list_tools()),
        "tools": [t.to_dict() for t in tool_registry.list_tools()],
        "schemas": tool_registry.list_schemas(),
    }


@app.post("/api/tools/call")
async def api_tools_call(body: dict):
    """直接调用工具（不经 LLM）。body: {name, params}"""
    from .tools import tool_registry
    name = body.get("name", "")
    params = body.get("params", {})
    result = tool_registry.call(name, params)
    # 设备状态变更后广播
    if result.get("ok"):
        await manager.broadcast({"type": "device_state", "devices": registry.get_all_states()})
    return result


@app.get("/api/tools/log")
async def api_tools_log():
    """获取工具调用日志（可审计）。"""
    from .tools import tool_registry
    return {"ok": True, "log": tool_registry.get_call_log()}


# ---------- Skills 技能系统 ----------
@app.get("/api/skills")
async def api_skills_list():
    """列出所有技能。"""
    from .skills import skill_manager
    return {"ok": True, "skills": skill_manager.list()}


@app.post("/api/skills")
async def api_skills_create(body: dict):
    """创建技能。body: {name, description, trigger_keywords, actions}"""
    from .skills import skill_manager
    skill_id = skill_manager.create(body)
    return {"ok": True, "id": skill_id}


@app.post("/api/skills/{skill_id}/execute")
async def api_skills_execute(skill_id: str):
    """手动执行技能。"""
    from .skills import skill_manager
    result = skill_manager.execute(skill_id, registry)
    if result.get("ok"):
        await manager.broadcast({"type": "device_state", "devices": registry.get_all_states()})
    return result


@app.delete("/api/skills/{skill_id}")
async def api_skills_delete(skill_id: str):
    """删除技能（内置技能不可删）。"""
    from .skills import skill_manager
    ok = skill_manager.delete(skill_id)
    return {"ok": ok}


# ---------- 端云协同（命题核心）----------
@app.post("/api/edge-cloud/route")
async def api_edge_cloud_route(body: dict):
    """端云路由演示：对输入做隐私分级，返回端/云路由决策。

    body: {text, sensors, modalities}
    """
    text = body.get("text", "")
    sensors = body.get("sensors") or agent.sensors
    modalities = body.get("modalities") or []
    route = agent.router.route(text, sensors, modalities)
    return {"ok": True, **route.to_dict()}


@app.get("/api/edge-cloud/audit")
async def api_edge_cloud_audit():
    """上云数据审计（数据最小化原则）：记录每条上云数据的脱敏情况。"""
    return {
        "ok": True,
        "upload_count": agent.router.guard.upload_count(),
        "audit": agent.router.guard.audit_snapshot(),
    }


@app.post("/api/edge-cloud/vision")
async def api_edge_cloud_vision(body: dict):
    """端侧视觉事件检测演示（画面本地处理，不上云）。

    body: {scene_hint: "跌倒"/"有人"/"无人"/"隐患"}
    """
    from .multimodal import VisionEventDetector
    detector = VisionEventDetector()
    event = detector.detect(body.get("scene_hint", ""))
    if event is None:
        return {"ok": True, "event": None, "alert": None, "note": "未检测到视觉事件"}
    return {"ok": True, "event": event.to_dict(), "alert": detector.event_to_alert(event)}


# ---------- MCP 协议接入 ----------
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP Server 端点（JSON-RPC 2.0 over HTTP）。
    外部 AI 客户端可通过此端点动态发现和调用智能家居工具。
    """
    from .mcp import MCPServer
    from .tools import tool_registry
    body = await request.json()
    mcp_server = MCPServer(tool_registry)
    response = mcp_server.handle_request(body)
    if response is None:
        return JSONResponse({}, status_code=202)
    return JSONResponse(response)


@app.get("/mcp/health")
async def mcp_health():
    """MCP Server 健康检查。"""
    from .tools import tool_registry
    return {
        "ok": True,
        "mcp_version": "2024-11-05",
        "server": "lingxi-smart-home",
        "tools_count": len(tool_registry.list_tools()),
    }


# ---------- 多渠道接入 ----------
@app.post("/api/external/command")
async def api_external_command(body: dict):
    """外部 HTTP API 渠道：第三方系统通过此端点发送自然语言指令。
    body: {text, source, metadata}
    """
    from .channels import ChannelMessage, channel_manager
    msg = ChannelMessage(
        channel="http",
        source=body.get("source", "external_api"),
        text=body.get("text", ""),
        metadata=body.get("metadata", {}),
    )
    # Agent 决策内部已完成执行（多智能体 + 授权校验），设备状态经 on_change 自动广播
    result = agent.decide(msg.text)
    return {
        "ok": True,
        "reply": result.get("reply", ""),
        "actions": result.get("actions", []),
        "results": result.get("results", []),
        "channel": "http",
    }


@app.post("/api/webhook/{source}")
async def api_webhook(source: str, request: Request):
    """Webhook 渠道：支持飞书/微信/钉钉等平台的消息推送。
    目前为通用 webhook，解析 JSON 中的 text 字段。
    """
    from .channels import ChannelMessage
    body = await request.json()
    text = body.get("text") or body.get("content") or body.get("msg", "")
    msg = ChannelMessage(channel="webhook", source=source, text=text, metadata=body)
    result = agent.decide(text)
    return {"ok": True, "reply": result.get("reply", ""), "actions": result.get("actions", []), "source": source}


@app.post("/api/mqtt/publish")
async def api_mqtt_publish(body: dict):
    """MQTT 模拟渠道：模拟 IoT 设备通过 MQTT 上报状态或接收指令。
    body: {topic, payload}
    """
    from .channels import channel_manager
    topic = body.get("topic", "home/command")
    payload = body.get("payload", {})
    channel_manager.mqtt_publish(topic, payload)
    reply = ""
    if "command" in payload:
        result = agent.decide(payload.get("command", ""))
        reply = result.get("reply", "")
    return {"ok": True, "topic": topic, "delivered": True, "reply": reply}


@app.get("/api/channels/status")
async def api_channels_status():
    """查询各渠道状态和统计。"""
    from .channels import channel_manager
    return {"ok": True, **channel_manager.get_stats()}


@app.post("/api/tts")
async def api_tts(body: dict):
    """文本转语音（Edge-TTS，中文神经网络音色）。返回 MP3 音频流。"""
    if not TTS_AVAILABLE:
        return JSONResponse({"ok": False, "error": "edge-tts 未安装"}, status_code=503)
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text 不能为空"}, status_code=400)
    voice = body.get("voice", "zh-CN-YunxiNeural")
    import os, sys, tempfile, subprocess
    tmp_mp3 = tempfile.mktemp(suffix=".mp3")
    tmp_txt = tempfile.mktemp(suffix=".txt")
    try:
        # 文本写临时文件，避免 Windows 命令行中文编码乱码
        with open(tmp_txt, "w", encoding="utf-8") as f:
            f.write(text)
        # 用同步子进程 + 线程池，避免 Windows 上 SelectorEventLoop 不支持子进程的问题
        def _run_tts():
            return subprocess.run(
                [sys.executable, "-m", "edge_tts",
                 "--file", tmp_txt, "--voice", voice,
                 "--write-media", tmp_mp3],
                capture_output=True, timeout=30,
            )
        result = await asyncio.to_thread(_run_tts)
        if result.returncode != 0 or not os.path.exists(tmp_mp3):
            err = result.stderr.decode("utf-8", errors="ignore")[:300] if result.stderr else "unknown"
            print("[TTS ERROR]", err)
            return JSONResponse({"ok": False, "error": f"TTS 生成失败: {err}"}, status_code=500)
        with open(tmp_mp3, "rb") as f:
            audio_data = f.read()
        if not audio_data:
            return JSONResponse({"ok": False, "error": "TTS 音频为空"}, status_code=500)
        return Response(content=audio_data, media_type="audio/mpeg")
    except Exception as e:
        print("[TTS ERROR]", f"{type(e).__name__}: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        for p in (tmp_mp3, tmp_txt):
            if os.path.exists(p):
                try: os.unlink(p)
                except: pass


# ---------------- WebSocket ----------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # 连接即推送初始状态
    await ws.send_text(json.dumps({"type": "init", "devices": registry.snapshot(),
                                   "sensors": simulator.values}, ensure_ascii=False))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")

            if mtype == "chat":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                result = await asyncio.to_thread(agent.decide, text)
                await ws.send_text(json.dumps({
                    "type": "agent_reply",
                    "reply": result["reply"],
                    "digital_human": result["digital_human"],
                    "actions": result["actions"],
                    "decision_trace": result.get("decision_trace", []),
                    "requires_confirmation": result.get("requires_confirmation", False),
                    "pending_id": result.get("pending_id", ""),
                    "pending_action": result.get("pending_action"),
                }, ensure_ascii=False))
            elif mtype == "confirm":
                pending_id = msg.get("pending_id", "")
                result = agent.confirm(pending_id)
                await ws.send_text(json.dumps({"type": "confirm_result", "result": result}, ensure_ascii=False))
            elif mtype == "scene":
                scene = msg.get("scene", "")
                if scene in SCENES:
                    for a in SCENES[scene]["actions"]:
                        registry.control(a["device_id"], a["command"], a.get("params", {}))
                    await ws.send_text(json.dumps({"type": "scene", "scene": scene,
                                                   "desc": SCENES[scene]["desc"]}, ensure_ascii=False))
            elif mtype == "control":
                result = registry.control(msg.get("device_id", ""), msg.get("command", ""), msg.get("params", {}))
                await ws.send_text(json.dumps({"type": "control_result", "result": result}, ensure_ascii=False))
            elif mtype == "edge":
                registry.set_state(msg.get("device_id", ""), msg.get("state", "offline"))
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ---------------- 前端静态托管 ----------------
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
