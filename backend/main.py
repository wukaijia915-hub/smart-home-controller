# -*- coding: utf-8 -*-
"""智能家居数字人中枢 —— 后端入口。
FastAPI + WebSocket：REST 查询 + 实时指令/状态推送 + 前端静态托管。
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .agent import HomeAgent
from .devices.registry import DeviceRegistry
from .scenes import SCENES
from .sensors import SensorSimulator
from .version import APP_VERSION, version_info

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


# ---- 能力注册表 + 事件路由引擎（可执行数字孪生 + 证据链闭环） ----
from .capabilities import CapabilityRegistry
from .event_routes import EventRouter

registry = DeviceRegistry()
manager = ConnectionManager()

capability_registry = CapabilityRegistry()
# 启动即配对孪生：为所有实体协议设备建立「虚拟预期态 ↔ 实体实测态」对照关系。
# 不做这一步的话 twin_pairs 恒为空 → registry.control() 的 result["twin"] 永不出现，
# 即「可执行数字孪生」在运行的系统里是死的（仅 tests/demo 手动配对时才活）。
_twin_paired = capability_registry.auto_pair(registry.all())

# 能力注册表在 Agent 之前创建：Root 的漏斗⑤ 与 Manager 的能力查表都要用它
agent = HomeAgent(registry, capabilities=capability_registry)
simulator = SensorSimulator(registry)

# 初始化标准化工具注册表，并装配执行护栏。
# 工具路径（/api/tools/call、MCP tools/call）与对话路径共用 Manager 的同一条护栏链
# （参数校验 → 租约签发 → 版本校验 → 高危挂起 → 回读确认 → 写审计），
# 不装配护栏则设备类工具一律拒绝执行（fail closed）。
from .tools import tool_registry
from .toolkit.guard import ManagerGuard
tool_registry.build_from_devices(registry)
# 单一执行入口：同一份 ManagerGuard 也被 /api/control、/api/scene、WS control
# 与技能执行复用。原因是此前这些入口直接调 registry.control()，绕过了租约、
# 版本校验与高危二次确认 —— 同一个洞在工具层修过一次（D22），其余入口还开着。
manager_guard = ManagerGuard(agent.manager)
tool_registry.bind_actuation_guard(manager_guard)

# state_store 必须注入：否则事件路径改状态不递增版本号，租约版本会与真相源脱节
event_router = EventRouter(registry, manager, state_store=agent.store)

# ---- 草稿→确认（规则结构级二次确认）----
# 与 StateStore.pending（动作级、10 秒）分开：草稿是"要不要创建这条规则"，
# 10 分钟有效、不落盘（重启即丢，避免幽灵草稿复活）。
from .drafts import draft_store

_started_at = time.time()

# ---- 感知层（多模态置信度融合 + 白盒情境参数） ----
from .perception import PerceptionContext, PerceptionService, PresenceTracker
from .perception.room_service import RoomService

perception_service = PerceptionService()
presence = PresenceTracker()          # 「在家时长」的真值来源，由 sensor_loop 喂入
perception_state: dict = {}           # 最近一次感知结果（前端轮询）

# ---- 房间服务：CSI 节点 → 房间四态 → 六路旁证 → 事件（v2.0） ----
# 未启用（wifi_csi.enabled=false）时它只提供空摘要，感知层行为与旧版一致。
room_service = RoomService(registry, agent.store,
                           sensors_getter=lambda: simulator.values)

# ---- L5 自治（v2.0）：偏好带 → 目标仲裁 → 四道闸 → 冷启动/漂移降级 ----
# 默认 enabled=false：不配置就不自动调设备，符合"演示优先、安全默认"。
from .adaptation import AdaptationService

adaptation_service = AdaptationService(
    registry, agent.store,
    sensors_getter=lambda: simulator.values,
    rooms_getter=lambda: room_service.summary())

# ---- 内环告警（声明式阈值 → 结构化事件 → 事件路由） ----
# 阈值在 config/alert_routes.json，联动方式在 config/event_routes.json。
# 原来这里是成排的 `if s.get("temp_indoor", 25) > 30`，阈值散在代码里、改一次要重启。
from .alerts import AlertEvaluator
from .config_loader import load_loops

alert_evaluator = AlertEvaluator()

# ---- 外环：22:00 家庭日报（LLM 优先 / 模板降级） ----
from .jobs import daily_summary

# ---- 记忆与观测 ----
from .memory import experience as experience_mod
from .memory import habits as habits_mod
from .memory import manuals as manuals_mod
from .memory.experience import derive_rating
from .observability import api_log as api_log_mod
from .observability import rate_limit as rate_limit_mod
from .observability.llm_log import llm_log


def _device_name(device_id: str) -> str:
    """device_id → 显示名。记忆层不导入设备注册表（那会让它有读世界的能力），
    显示名由这里注入。"""
    d = registry.get(device_id)
    return getattr(d, "name", device_id) if d else device_id


def _run_daily_summary(window_hours: float = 24.0) -> dict:
    """跑一次外环日报。定时任务与手动触发**共用这一条路径**。

    两条路径分开写的话，定时那条永远没有测试覆盖（演示环境跑不到 22:00），
    而它恰恰是唯一会在无人值守时执行的那条。
    """
    return daily_summary.run(
        registry, agent.store, agent.llm, perception_state, window_hours=window_hours)


def _presence_state(rooms: dict) -> str:
    """把房间四态归并成一个聚合在场态（给感知上下文用）。

    归并顺序是刻意的：present 优先（有正向证据），degraded 次之（不可推断），
    只有**所有**房间都 empty_confident 才算 empty，其余一律 uncertain。
    """
    states = [str((r or {}).get("state", "")) for r in (rooms or {}).values()]
    if not states:
        return "uncertain"
    if any(s == "present_confident" for s in states):
        return "present"
    if any(s == "degraded" for s in states):
        return "degraded"
    if all(s == "empty_confident" for s in states):
        return "empty"
    return "uncertain"


def _perception_context() -> PerceptionContext:
    """从当前系统状态组装感知上下文。"""
    recent = agent.store.get_audit(limit=30)
    rooms = room_service.summary()
    return PerceptionContext(
        sensors=simulator.values,
        recent_actions=recent,
        habit={"routine_deviation": 1.0},   # 无历史基线时取中性值，置信度会相应下调
        presence_minutes=presence.minutes,
        idle_minutes=presence.idle_minutes(),
        rooms=rooms,
        presence_state=_presence_state(rooms),
    )


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
    event_router.set_loop(_loop)   # 事件路由的数字人播报需要主循环引用

    loops = load_loops().get("inner_loop", {})
    SENSOR_SECONDS = float(loops.get("sensor_seconds", 2.0))
    ALERT_SECONDS = float(loops.get("alert_seconds", 5.0))
    PERCEPTION_SECONDS = float(loops.get("perception_seconds", 15.0))
    ROOM_SECONDS = float(loops.get("room_seconds", 5.0))
    ADAPTATION_SECONDS = float(loops.get("adaptation_seconds", 60.0))
    SUGGESTION_COOLDOWN = float(loops.get("suggestion_cooldown_seconds", 300.0))

    # CSI 接入：enabled=false 或未装 paho 时返回原因，不阻塞主服务
    ingest = room_service.start_ingest()
    print(f"[CSI] MQTT 接入：{'已启动' if ingest.get('started') else ingest.get('last_error', '未启动')}")

    # 传感器变化 → 广播
    async def sensor_loop():
        while True:
            await asyncio.sleep(SENSOR_SECONDS)
            simulator.tick()
            agent.sensors = simulator.values  # 注入 Agent 环境感知
            # 喂感知层：PresenceTracker 需要时序才能算出「在家时长」
            present = bool(simulator.values.get("motion_living")
                           or simulator.values.get("motion_bedroom"))
            presence.update(present)
            # 喂外环：日报的「最低/平均/最高」必须来自真实采样，不能拿当前读数冒充
            daily_summary.series.tick(simulator.values)
            # 记忆层延迟落盘：习惯统计不是事务数据，没必要每条动作都重写整个文件。
            # 由内环此处统一 flush（见 memory/habits.py 的说明：实测这一项占了
            # 规则层延迟的 98%，8.74ms → 0.13ms）。
            try:
                habits_mod.habits.flush_if_dirty()
            except Exception as e:
                print(f"[记忆·习惯] 落盘失败（不影响控制链路）: {type(e).__name__}: {e}")
            await manager.broadcast({"type": "sensor", "data": simulator.values})

    # 内环告警：读数 → 声明式阈值判定 → 结构化事件 → 事件路由（联动 + 播报）
    #
    # 这里**只做确定性阈值判定**（`temp_indoor > 30`），阈值在
    # config/alert_routes.json 里可复核。它与感知层外观相似但性质不同：
    # 感知层是多模态统计推断，因此感知层绝不允许触发设备动作（红线 1），
    # 而本层允许。二者混为一谈会导致「既然环境告警能自动开空调，
    # 为什么情绪判断不能自动调灯」这种答辩场上的致命追问。
    async def alert_loop():
        while True:
            await asyncio.sleep(ALERT_SECONDS)
            try:
                events = alert_evaluator.evaluate(simulator.values, registry.all())
            except Exception as e:
                print(f"[内环告警] 评估失败（不影响设备控制）: {type(e).__name__}: {e}")
                continue
            for event in events:
                try:
                    # 与 /api/events/route 同一条路径：设备动作要过回读确认与审计，
                    # 且高危命令会被 EventRouter 默认拒绝（见 event_routes._refuse_high_risk）
                    evaluation = await asyncio.to_thread(event_router.route, event)
                    _record_alert_experience(event, evaluation)
                except Exception as e:
                    print(f"[内环告警] 路由失败 {event.get('type')}: {type(e).__name__}: {e}")

    # 房间环：节点 sweep → 四态融合 → 六路旁证 → 状态变化事件。
    # 关键：sweep 必须由定时循环驱动，不能等下一帧 —— 节点死了不会再有消息，
    # 等消息就等于永远发现不了它掉线（见 node_registry.py）。
    async def room_loop():
        while True:
            await asyncio.sleep(ROOM_SECONDS)
            try:
                result = await asyncio.to_thread(room_service.tick)
            except Exception as e:
                print(f"[房间环] 评估失败（不影响设备控制）: {type(e).__name__}: {e}")
                continue
            for event in result.get("events", []):
                try:
                    # 只有 empty_confident 才会产生 no_person；uncertain 永不触发
                    await asyncio.to_thread(event_router.route, event)
                except Exception as e:
                    print(f"[房间环] 事件路由失败 {event.get('type')}: {type(e).__name__}: {e}")
            try:
                await manager.broadcast({"type": "rooms",
                                         "rooms": result.get("rooms", {}),
                                         "events": result.get("events", [])})
            except Exception:
                pass

    # L5 自治环：默认 enabled=false，因此这里即使跑也只是"算建议、不动作"。
    async def adaptation_loop():
        while True:
            await asyncio.sleep(ADAPTATION_SECONDS)
            try:
                result = await asyncio.to_thread(adaptation_service.tick)
                if result.get("applied", {}).get("ok"):
                    await manager.broadcast({"type": "adaptation",
                                             "result": result.get("applied", {})})
            except Exception as e:
                print(f"[L5 自治] 评估失败（不影响设备控制）: {type(e).__name__}: {e}")

    def _record_alert_experience(event: dict, evaluation) -> None:
        """把环境联动记入经验库（category=active_alert）。

        日报里"今天触发了 N 次环境联动"就来自这里；同时也让经验库不只服务
        "模糊指令"一类。评分同样来自**执行结果**，不是用户满意度。
        """
        try:
            actions = [a for a in evaluation.actions if a.get("type") == "device"]
            if not actions:
                return
            ok = sum(1 for a in actions if a.get("ok"))
            payload = event.get("payload") or {}
            experience_mod.experience.record(
                category="active_alert",
                context_key=payload.get("kind", ""),
                request=payload.get("summary", event.get("type", "")),
                actions=actions,
                rating=derive_rating(ok, len(actions)),
                source="event_route",
            )
        except Exception:
            pass

    # 设备变更 → 广播（可能来自 asyncio.to_thread 工作线程）
    def on_device_change(device, command, result):
        if _loop is not None:
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "device_state", "device": device.to_dict()}), _loop
            )

    registry.on_change(on_device_change)

    # 初始化定时任务调度器（apscheduler 为可选依赖：缺失时降级而非启动失败）
    inner_scheduler = None
    try:
        from .scheduler import init_scheduler, add_internal_job
        init_scheduler(app, registry, manager)
        inner_scheduler = True
    except ImportError as e:
        print(f"[调度器] 未启用定时任务（缺少依赖 {e}），请 pip install apscheduler")

    # 外环：每日 22:00 生成家庭日报（时刻在 config/loops.json，可现场改）
    outer = load_loops().get("outer_loop", {})
    if inner_scheduler and outer.get("enabled", True):
        try:
            from .scheduler import add_internal_job

            async def _daily_job():
                report = await asyncio.to_thread(_run_daily_summary)
                await manager.broadcast({"type": "daily_summary", "report": report})
                print(f"[外环·日报] 已生成（{report.get('source')}，"
                      f"{report.get('duration_ms')}ms）")

            add_internal_job(
                "daily_summary", _daily_job,
                hour=int(outer.get("cron_hour", 22)),
                minute=int(outer.get("cron_minute", 0)),
                timezone=str(outer.get("timezone", "Asia/Shanghai")))
            print(f"[外环·日报] 已注册：每天 {outer.get('cron_hour', 22):02d}:"
                  f"{outer.get('cron_minute', 0):02d}（{outer.get('timezone', 'Asia/Shanghai')}）")
        except Exception as e:
            print(f"[外环·日报] 注册失败（不影响其它功能）: {type(e).__name__}: {e}")

    # 感知层周期评估。
    # ⚠️ 这里**只广播询问式建议，绝不触发任何设备动作** —— 感知结果进入控制回路
    # 是本项目的硬边界（见 backend/perception/__init__.py 的 PERCEPTION_CANNOT_ACTUATE）。
    # 同一句建议不重复播报，且带冷却，避免变成骚扰。
    last_suggestion = {"text": "", "at": 0.0}
    async def perception_loop():
        while True:
            await asyncio.sleep(PERCEPTION_SECONDS)
            try:
                result = perception_service.perceive(_perception_context())
            except Exception as e:
                print(f"[感知层] 评估失败（不影响其它功能）: {type(e).__name__}: {e}")
                continue

            perception_state.clear()
            perception_state.update(result.to_dict())

            text = result.suggestion
            if not text or text == last_suggestion["text"]:
                continue
            now = asyncio.get_running_loop().time()
            if now - last_suggestion["at"] < SUGGESTION_COOLDOWN:
                continue
            last_suggestion.update({"text": text, "at": now})
            await manager.broadcast({
                "type": "perception_suggestion",
                "suggestion": text,
                "basis": result.suggestion_basis,
                "situation": result.situation,
                # 前端据此确认：这是建议，不是已执行的动作
                "actionable_by_system": False,
            })

    task = asyncio.create_task(sensor_loop())
    alert_task = asyncio.create_task(alert_loop())
    perception_task = asyncio.create_task(perception_loop())
    room_task = asyncio.create_task(room_loop())
    adaptation_task = asyncio.create_task(adaptation_loop())
    yield
    task.cancel()
    alert_task.cancel()
    perception_task.cancel()
    room_task.cancel()
    adaptation_task.cancel()
    room_service.stop_ingest()
    # 关闭前补一次落盘：习惯数据改为延迟写入后，正常退出时不能丢最后几秒的计数
    try:
        habits_mod.habits.flush()
    except Exception as e:
        print(f"[记忆·习惯] 退出落盘失败: {type(e).__name__}: {e}")


app = FastAPI(title="犀鉴 · 基于 Wi-Fi CSI 的隐私优先家庭感知与可信自适应系统", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# 观测层中间件。挂载顺序：最后 add 的最先执行，因此 api_log 先于 rate_limit
# —— 被限流的请求（429）也要被记录，否则"谁在刷接口"这件事在日志里看不到。
api_log_mod._register_middleware(app)
rate_limit_mod._register_middleware(app)


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
    """一键应用场景。

    注意：场景里的**每一条**设备动作都走 `ManagerGuard`，与对话/工具同一条护栏链。
    此前这里直接 `registry.control()`，等于给场景留了一条绕过租约与审计的旁路。
    """
    scene = body.get("scene", "")
    if scene not in SCENES:
        return JSONResponse({"ok": False, "error": f"未知场景: {scene}"}, status_code=400)
    results = await asyncio.to_thread(_apply_scene_actions, scene)
    await manager.broadcast({"type": "scene", "scene": scene, "desc": SCENES[scene]["desc"]})
    return {"ok": True, "results": results,
            "requires_confirmation": any(r.get("requires_confirmation") for r in results)}


def _apply_scene_actions(scene: str) -> list[dict]:
    """场景动作串行执行（同一设备的动作顺序不能颠倒，回读证据才连贯）。"""
    return [manager_guard.actuate(a["device_id"], a["command"], a.get("params", {}))
            for a in SCENES[scene]["actions"]]


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
    """开放接入 SDK：外部直接控制设备。

    **不再直连 registry.control()**：走 ManagerGuard，于是与对话路径共享
    参数包络 / 租约 / 版本校验 / 高危二次确认 / 回读证据 / 审计。
    高危命令会返回 `requires_confirmation=True` 与 pending_id，需再调 /api/confirm。
    """
    result = await asyncio.to_thread(
        manager_guard.actuate,
        body.get("device_id", ""), body.get("command", ""), body.get("params", {}))
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
    # 配置缓存与自定义路由也要跟着重载，否则"改了 JSON 却要等 10s/重启"。
    from .devices.registry import invalidate_readback_settings
    from .toolkit.scoping import invalidate_scope_cache
    from .verification.claim_checker import invalidate_claim_cache
    invalidate_readback_settings()
    invalidate_scope_cache()
    invalidate_claim_cache()
    draft_store.reload_config()
    event_router.load()
    room_service.reload_config()
    adaptation_service.reload_config()
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


# ---------- 记忆层三源 ----------
@app.get("/api/memory/stats")
async def api_memory_stats():
    """记忆三源统计：手册 / 习惯 / 经验。

    经验库的 `rating` 如实区分来源：`auto_execution`（由执行结果推导，
    衡量"执行成功了吗"）与 `user`（用户显式反馈，衡量"用户满意吗"）。
    把两者混为一谈是最容易被问穿的地方。
    """
    return {
        "ok": True,
        "manuals": {"count": len(manuals_mod.manuals.manuals),
                    "path": str(manuals_mod.manuals.path)},
        "habits": habits_mod.habits.stats(name_of=_device_name),
        "experience": experience_mod.experience.stats(),
    }


@app.get("/api/memory/experience")
async def api_memory_experience(category: str = "", query: str = "", limit: int = 10):
    """检索经验库（与慢环 prompt 注入用的是**同一个确定性排序函数**，
    因此这里看到的顺序就是模型看到的顺序）。"""
    exp = experience_mod.experience
    entries = exp.retrieve(prefer_category=category, query=query, limit=limit, min_rating=0)
    return {"ok": True, "count": len(entries), "entries": entries,
            "stats": exp.stats()}


@app.post("/api/memory/feedback")
async def api_memory_feedback(body: dict):
    """用户显式评分：body {id, rating 1-5, feedback}。

    这是经验库里**唯一**衡量用户满意度的信号 —— 自动评分只反映执行结果。
    覆盖以追加同 id 记录的方式落盘（JSONL append-only，崩溃安全，且保留了
    "先 5 分后改成 3 分"这件事本身）。
    """
    ok = experience_mod.experience.rate(
        body.get("id", ""), int(body.get("rating", 0) or 0), body.get("feedback", ""))
    if not ok:
        return JSONResponse({"ok": False, "error": "id 或 rating(1-5) 不合法"}, status_code=400)
    return {"ok": True, "stats": experience_mod.experience.stats()}


# ---------- 可观测性 ----------
@app.get("/api/metrics/llm")
async def api_metrics_llm():
    """分层延迟与降级率 —— 「规则层 7ms + 慢环预算化降级」这套说法的取数来源。

    答辩时这一页用来回答"你们的响应时间到底多少"：规则层是纯内存查表，
    慢环是模型生成（≈1.2s + 输出 token ÷ 15），两者必须分开报。
    """
    return {
        "ok": True,
        **llm_log.metrics(budget_snapshot=agent.root.budget.snapshot()),
        "recent_calls": llm_log.recent_calls(limit=10),
        "llm_client": agent.llm.get_status(),
    }


@app.get("/api/metrics/api")
async def api_metrics_api():
    """接口层统计：端点分布、状态码、平均延迟、限流拒绝数。"""
    return {"ok": True, "api": api_log_mod.api_log.stats(),
            "rate_limit": rate_limit_mod.limiter.stats(),
            "perception": perception_service.status(),
            "rooms": room_service.summary(),
            "adaptation": adaptation_service.health(),
            "alerts": alert_evaluator.stats(),
            "store": agent.store.snapshot()}


# ---------- 内环告警（声明式阈值） ----------
@app.get("/api/alerts/config")
async def api_alerts_config():
    """当前生效的告警阈值配置（可复核：阈值不在代码里，在这个 JSON 里）。"""
    from .config_loader import load_alert_routes
    return {"ok": True, "config": load_alert_routes(), "stats": alert_evaluator.stats()}


@app.post("/api/alerts/evaluate")
async def api_alerts_evaluate(body: dict):
    """按给定读数**预演**一次告警判定（dry-run，不产生任何设备动作）。

    body: {sensors: {...}}，缺省用当前传感器读数。演示用：现场把温度说成 32℃
    就能看到"判定 → 事件"这一步，而不必真的把房间加热。
    """
    sensors = dict(simulator.values)
    if isinstance(body.get("sensors"), dict):
        sensors.update({k: v for k, v in body["sensors"].items()
                        if isinstance(v, (int, float, bool))})
    # 用**独立实例**预演：不能污染正在跑的告警循环的冷却状态，
    # 否则"预演一下"会把真实告警的冷却吃掉，导致真出事时反而不报。
    preview = AlertEvaluator()
    events = preview.evaluate(sensors, [])
    return {"ok": True, "sensors": sensors, "events": events}


# ---------- 外环：家庭日报 ----------
@app.get("/api/jobs/daily_summary/latest")
async def api_daily_latest():
    """最近一份家庭日报。"""
    return {"ok": True, "report": daily_summary.latest(), "stats": daily_summary.stats()}


@app.get("/api/jobs/daily_summary/history")
async def api_daily_history(limit: int = 10):
    return {"ok": True, "reports": daily_summary.history(limit=limit)}


@app.post("/api/jobs/daily_summary/run")
async def api_daily_run(body: dict | None = None):
    """手动触发日报（演示用：演示环境等不到 22:00）。

    与定时任务走**同一条** `_run_daily_summary`。window_hours 可缩短，
    用于演示"只统计最近 1 小时"。
    """
    body = body or {}
    hours = float(body.get("window_hours", 24.0) or 24.0)
    report = await asyncio.to_thread(_run_daily_summary, hours)
    await manager.broadcast({"type": "daily_summary", "report": report})
    return {"ok": report.get("ok", False), "report": report}


@app.get("/api/jobs/status")
async def api_jobs_status():
    """调度器状态：内部作业（日报）的下次触发时刻 + 用户定时任务数。"""
    try:
        from .scheduler import list_internal_jobs, list_tasks
        return {"ok": True, "internal_jobs": list_internal_jobs(),
                "user_tasks": len(list_tasks())}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


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


# ---------- 草稿 → 确认（对话建规则） ----------
def _draft_high_risk(actions: list) -> list[str]:
    from .drafts import draft_high_risk
    return draft_high_risk(actions, registry)


def _confirm_draft(draft_id: str, allow_high_risk: bool = False) -> tuple[bool, dict]:
    """REST 与 WS 共用这一个确认函数（不变式只有一份，见 backend/drafts.py）。"""
    from .drafts import confirm_draft
    return confirm_draft(draft_store, event_router, registry, draft_id, allow_high_risk)


@app.get("/api/drafts")
async def api_drafts(session: str = ""):
    """列出待确认的规则草稿（草稿不落盘，进程重启即丢）。"""
    return {"ok": True, "drafts": draft_store.list(session or None),
            "stats": draft_store.stats()}


@app.get("/api/drafts/{draft_id}")
async def api_draft_get(draft_id: str):
    draft = draft_store.get(draft_id)
    if draft is None:
        return JSONResponse({"ok": False, "error": "草稿不存在或已过期"}, status_code=404)
    return {"ok": True, "draft": draft.to_dict(),
            "high_risk": _draft_high_risk((draft.payload or {}).get("actions") or [])}


@app.post("/api/drafts/{draft_id}/confirm")
async def api_draft_confirm(draft_id: str, body: dict | None = None):
    """确认草稿 → 写入自定义事件路由（此后才生效）。

    body 可选 `allow_high_risk: true`：草稿含高危动作时必须显式给出。
    """
    body = body or {}
    ok, payload = _confirm_draft(draft_id, bool(body.get("allow_high_risk", False)))
    if not ok:
        status = 404 if payload.get("code") == "draft_not_found" else 400
        return JSONResponse({"ok": False, **payload}, status_code=status)
    return {"ok": True, **payload}


@app.post("/api/drafts/{draft_id}/cancel")
async def api_draft_cancel(draft_id: str):
    ok = draft_store.cancel(draft_id)
    return {"ok": ok, **({} if ok else {"error": "草稿不存在或已过期"})}


@app.get("/api/event-routes")
async def api_event_routes():
    """当前生效的事件路由：预置（只读）+ 用户创建（可删）。"""
    event_router.load()
    return {
        "ok": True,
        "base": [{"id": r.get("id"), "desc": r.get("desc", ""), "editable": False}
                 for r in event_router.list_base()],
        "custom": [{**r, "editable": True} for r in event_router.list_custom()],
    }


@app.delete("/api/event-routes/{route_id}")
async def api_event_route_delete(route_id: str):
    ok = event_router.remove_route(route_id)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": "只允许删除用户创建的规则（预置路由请改 config/event_routes.json）"},
            status_code=404)
    return {"ok": True, "removed": route_id}


# ---------- 存活探针与版本 ----------
def _data_dir_writable() -> bool:
    import os
    from .config_loader import data_dir
    try:
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        return os.access(d, os.W_OK)
    except Exception:
        return False


def _scheduler_ready() -> bool:
    try:
        from .scheduler import list_internal_jobs
        list_internal_jobs()
        return True
    except Exception:
        return False


@app.get("/healthz")
async def healthz():
    """**无认证**存活探针（容器 healthcheck 用）。

    为什么单独一个无认证端点：Docker 的 healthcheck 跑在容器内、不带登录态。
    对照项目 Aether 的经验是，`restart: unless-stopped` 只能救"进程退出"，
    救不了"进程活着但事件循环冻结"——灵犀把所有后台循环挂在一个 loop 上，
    同一个风险存在，所以必须有一个能被容器直接打到的探针。

    这里只返回是否活着与极少量状态，不泄露设备/家庭信息。
    """
    return {
        "ok": True,
        "service": "lingxi-smart-home",
        "version": APP_VERSION,
        "uptime_s": round(time.time() - _started_at, 1),
        "checks": {
            "data_dir_writable": _data_dir_writable(),
            "llm_configured": bool(getattr(agent.llm, "available", False)),
            "scheduler": _scheduler_ready(),
        },
    }


@app.get("/api/health")
async def api_health():
    """诊断用健康信息（无敏感字段，供运维页/诊断包引用）。"""
    return {
        "ok": True,
        **version_info(),
        "uptime_s": round(time.time() - _started_at, 1),
        "state_version": agent.store.version,
        "devices": len(registry.all()),
        "tools": len(tool_registry.list_tools()),
        "drafts": draft_store.stats(),
        "llm": agent.llm.get_status() if hasattr(agent.llm, "get_status") else {},
    }


# ---------- 信任层：六段链 / 撤销 ----------
@app.get("/api/trust/recent")
async def api_trust_recent(limit: int = 20):
    """最近决策摘要 + 可撤销动作列表（前端信任面板的数据源）。"""
    from .trust import explain_recent, undo_store
    return {"ok": True,
            "decisions": explain_recent(agent.store, limit=limit),
            "undoable": undo_store.recent(limit=limit),
            "stats": {"decisions": len(agent.store.decisions),
                      "audit": len(agent.store.audit_log)}}


@app.get("/api/trust/undo/{action_id}")
async def api_trust_undo_check(action_id: str):
    """撤销前置检查：能否撤销、原因、剩余秒数、将下发的逆动作。"""
    from .trust import can_undo
    chk = can_undo(action_id, registry)
    return {"ok": bool(chk.get("ok")), **chk}


@app.post("/api/trust/undo/{action_id}")
async def api_trust_undo(action_id: str):
    """执行撤销：走 execution.execute_plan(source="undo")，不是直连设备。"""
    from .trust import apply_undo
    result = await asyncio.to_thread(apply_undo, action_id,
                                     registry=registry, store=agent.store)
    if not result.get("ok"):
        return JSONResponse({"ok": False, **result}, status_code=400)
    return {"ok": True, **result}


@app.get("/api/trust/{decision_id}")
async def api_trust_detail(decision_id: str):
    """一条决策的六段链 + 失败归因；滚出缓冲时 404 并说明原因。"""
    from .trust import explain
    detail = explain(agent.store, decision_id)
    if not detail.get("ok"):
        return JSONResponse(detail, status_code=404)
    return detail


# ---------- L5 自治（v2.0） ----------
@app.get("/api/adaptation")
async def api_adaptation():
    """偏好带、目标提议、四道闸结果、自治等级与冷启动状态。"""
    return {"ok": True, **adaptation_service.status()}


@app.get("/api/adaptation/health")
async def api_adaptation_health():
    """自治健康：等级、模式、纠正率/自动执行占比/感知降级比例与阈值。"""
    return {"ok": True, **adaptation_service.health()}


@app.post("/api/adaptation/tick")
async def api_adaptation_tick(body: dict | None = None):
    """手动推进一轮 L5 评估（演示用；enabled=false 时只出建议）。"""
    now = (body or {}).get("now")
    result = await asyncio.to_thread(adaptation_service.tick, now)
    return {"ok": True, **result}


@app.post("/api/adaptation/{proposal_id}/accept")
async def api_adaptation_accept(proposal_id: str):
    """接受一条建议（走 Manager/租约链执行，不直连设备）。"""
    result = await asyncio.to_thread(adaptation_service.accept, proposal_id)
    return result


@app.post("/api/adaptation/{proposal_id}/reject")
async def api_adaptation_reject(proposal_id: str):
    """否决一条建议：写入训练信号（纠正率），不执行。"""
    return adaptation_service.reject(proposal_id)


# ---------- 标准化工具注册表 ----------
@app.get("/api/tools")
async def api_tools_list(intent: str = ""):
    """列出已注册工具（含 schema）。

    `?intent=卧室灯` 可按意图裁剪（高危命令默认不可见）。不传 intent 时返回全量，
    保持旧调用方兼容 —— 全量与裁剪的区别会体现在 `scope` 字段里。
    """
    from .tools import tool_registry
    tools, scope = tool_registry.list_tools_scoped(intent)
    return {
        "ok": True,
        "total": len(tools),
        "all_total": len(tool_registry.list_tools()),
        "scope": scope,
        "tools": [t.to_dict() for t in tools],
        "schemas": [t.to_schema() for t in tools],
    }


@app.get("/api/tools/scope")
async def api_tools_scope(intent: str = ""):
    """演示/复核用：一句话会把设备清单与工具集裁到多少。

    答辩可直接跑：
        /api/tools/scope?intent=卧室太亮    → 17→3 台设备、64→12 个工具
        /api/tools/scope?intent=解锁门锁    → 高危命令因显式提到"解锁"才出现
    """
    from .toolkit.scoping import describe_scope, select_devices
    from .tools import tool_registry
    tools, tools_meta = tool_registry.list_tools_scoped(intent)
    _, devices_meta = select_devices(intent, registry.all())
    return {
        "ok": True,
        "intent": intent,
        "devices": devices_meta,
        "tools": tools_meta,
        "summary": describe_scope(devices_meta),
        "tool_names": [t.name for t in tools],
    }


@app.post("/api/tools/call")
async def api_tools_call(body: dict):
    """直接调用工具（不经 LLM）。body: {name, params}"""
    from .tools import tool_registry
    name = body.get("name", "")
    params = body.get("params", {})
    # 设备状态变更已由 registry.on_change 回调自动广播，此处无需重复广播
    return tool_registry.call(name, params)


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
    """手动执行技能。

    技能内的动作交给 `manager_guard.actuate` 逐条执行（与对话/工具同一条护栏链），
    技能层自身仍保留"高危命令不得批量执行"的拒绝 —— 两道闸互不替代。
    """
    from .skills import skill_manager
    return await asyncio.to_thread(skill_manager.execute, skill_id, registry,
                                   manager_guard.actuate)


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


# ---------- 感知层（多模态融合 + 白盒情境参数） ----------
@app.get("/api/perception")
async def api_perception():
    """最近一次感知结果（前端情境看板轮询）。

    返回各模态读数、融合门控结果、5 个白盒情境参数（含真值来源与原始依据）、
    以及**询问式**建议。suggestion 为 None 表示当前无需打扰。
    """
    return {"ok": True, **(perception_state or {"note": "尚无感知结果，等待首个评估周期"})}


@app.get("/api/perception/status")
async def api_perception_status():
    """感知层状态：各模态可用性、门控阈值、算力后端。"""
    from .perception.gpu import status as gpu_status
    return {"ok": True, **perception_service.status(), "accelerator": gpu_status()}


@app.post("/api/perception/observe")
async def api_perception_observe(body: dict):
    """按给定上下文跑一次感知（演示/测试用，不影响后台周期评估）。

    body（全部可选）：
        scene_hint        端侧视觉检测提示，如"跌倒"/"有人"/"无人"
        sensors           传感器读数覆盖
        recent_actions    近期操作条数（用整数即可，如 5）
        presence_minutes  在家时长（分钟）
        idle_minutes      距上次活动分钟数
        vestibular        前庭读数（可选）
        face_frame        是否提供画面帧（bool；真实帧需另走接口）
        now_hour          指定判定时刻的小时数 0-23（可选）。
                          存在的理由有两个：①「深夜活动」规则的生效时段是
                          23:00-05:00，演示时不可能等到深夜，用它可以现场复现；
                          ②测试必须能把时钟钉死，否则同一份断言会随跑测试的
                          钟点而红绿（阶段 3 的测试就踩过：22:00 绿、23:01 红）。

    返回建议为**问句或 None**，绝不会产生设备动作。
    """
    sensors = body.get("sensors")
    sensors = dict(simulator.values) if sensors is None else dict(sensors)
    count = body.get("recent_actions", 0)
    recent = [{"source": "observe"}] * int(count if isinstance(count, int) else 0)

    now = None
    if isinstance(body.get("now_hour"), (int, float)):
        from datetime import datetime
        hour = max(0, min(23, int(body["now_hour"])))
        now = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)

    ctx = PerceptionContext(
        scene_hint=body.get("scene_hint", ""),
        sensors=sensors,
        recent_actions=recent,
        habit=body.get("habit") or {},
        presence_minutes=float(body.get("presence_minutes", presence.minutes)),
        idle_minutes=float(body.get("idle_minutes", presence.idle_minutes())),
        lying_minutes=float(body.get("lying_minutes", 0.0)),
        vestibular=body.get("vestibular") or {},
        face_frame=object() if body.get("face_frame") else None,
        now=now,
    )
    result = perception_service.perceive(ctx)
    return {"ok": True, **result.to_dict()}


# ---------- 房间四态 / CSI 接入（v2.0） ----------
@app.get("/api/rooms")
async def api_rooms():
    """全部房间的四态、置信度、证据来源与节点在线情况。"""
    return {"ok": True, "summary": room_service.summary(), **room_service.status()}


@app.get("/api/rooms/{room_id}/timeline")
async def api_room_timeline(room_id: str, limit: int = 50):
    """房间状态变化时间线（只在状态变化时追加，不含原始 CSI）。"""
    return {"ok": True, "room_id": room_id,
            "timeline": room_service.timeline_recent(room_id, limit=limit)}


@app.get("/api/csi/status")
async def api_csi_status():
    """CSI 接入状态：enabled / paho / MQTT 连接 / 节点表 / 统计。"""
    return {"ok": True, **room_service.status()}


@app.post("/api/rooms/tick")
async def api_rooms_tick(body: dict | None = None):
    """手动推进一轮房间评估（演示/测试用，不必等 5 秒循环）。"""
    now = (body or {}).get("now")
    result = await asyncio.to_thread(room_service.tick, now)
    for event in result.get("events", []):
        await asyncio.to_thread(event_router.route, event)
    return {"ok": True, **result}


@app.post("/api/csi/replay")
async def api_csi_replay(body: dict):
    """回放节点事件（无硬件演示）：body.records = [{node_id,state,received_at}]。

    回放产生的事件与实时路径走同一个 EventRouter（no_person → 离家节能）。
    """
    records = body.get("records") or []
    result = await asyncio.to_thread(room_service.replay, records)
    for event in result.get("events", []):
        await asyncio.to_thread(event_router.route, event)
    return {"ok": True, **result}


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
                    "results": result.get("results", []),
                    "verification": result.get("verification", {}),
                    "claim_issues": result.get("claim_issues", []),
                    "scope": result.get("scope", {}),
                    "draft": result.get("draft"),
                    "decision_trace": result.get("decision_trace", []),
                    "requires_confirmation": result.get("requires_confirmation", False),
                    "pending_id": result.get("pending_id", ""),
                    "pending_action": result.get("pending_action"),
                }, ensure_ascii=False))
            elif mtype == "confirm":
                pending_id = msg.get("pending_id", "")
                result = agent.confirm(pending_id)
                await ws.send_text(json.dumps({"type": "confirm_result", "result": result}, ensure_ascii=False))
            elif mtype == "confirm_draft":
                # 草稿确认与 REST 共用 _confirm_draft：不变式只有一份
                ok, payload = _confirm_draft(msg.get("draft_id", ""),
                                             bool(msg.get("allow_high_risk", False)))
                await ws.send_text(json.dumps(
                    {"type": "draft_confirm_result", "ok": ok, **payload}, ensure_ascii=False))
            elif mtype == "scene":
                scene = msg.get("scene", "")
                if scene in SCENES:
                    await asyncio.to_thread(_apply_scene_actions, scene)
                    await ws.send_text(json.dumps({"type": "scene", "scene": scene,
                                                   "desc": SCENES[scene]["desc"]}, ensure_ascii=False))
            elif mtype == "control":
                result = await asyncio.to_thread(
                    manager_guard.actuate, msg.get("device_id", ""),
                    msg.get("command", ""), msg.get("params", {}))
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
