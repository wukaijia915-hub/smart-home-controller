"""定时任务调度器：支持 cron 表达式和简单时间设置，触发场景或设备命令。"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import json, os, logging
from datetime import datetime

logger = logging.getLogger("scheduler")

_scheduler = None
_tasks = {}  # task_id -> task config
_registry = None   # 设备注册表引用（init_scheduler 注入）
_manager = None    # 连接管理器引用（init_scheduler 注入）
TASKS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "scheduled_tasks.json")


def _load_tasks():
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_tasks():
    os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(_tasks, f, ensure_ascii=False, indent=2)


def init_scheduler(app, registry=None, manager=None):
    """初始化调度器，恢复持久化的任务。"""
    global _scheduler, _registry, _manager
    _registry = registry
    _manager = manager
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    _scheduler.start()
    saved = _load_tasks()
    for tid, cfg in saved.items():
        try:
            _add_job(tid, cfg)
            _tasks[tid] = cfg
        except Exception as e:
            logger.warning(f"恢复定时任务失败 {tid}: {e}")
    logger.info(f"定时任务调度器已启动，恢复 {len(_tasks)} 个任务")


def _add_job(task_id: str, cfg: dict):
    """添加一个 cron 任务。"""
    trigger = CronTrigger(
        hour=cfg.get("hour", "*"),
        minute=cfg.get("minute", "*"),
        second=cfg.get("second", "0"),
        timezone="Asia/Shanghai",
    )
    _scheduler.add_job(
        _execute_task,
        trigger=trigger,
        id=task_id,
        replace_existing=True,
        args=[task_id, cfg],
    )


async def _execute_task(task_id: str, cfg: dict):
    """执行定时任务：触发场景或发送设备命令。

    设备状态变更经 registry.on_change 回调自动广播，这里只广播一条数字人提示。
    """
    from .scenes import SCENES
    logger.info(f"执行定时任务: {cfg.get('name', task_id)}")
    if _registry is None:
        return
    reply = None
    if cfg.get("action_type") == "scene":
        scene = cfg.get("scene")
        if scene and scene in SCENES:
            for a in SCENES[scene]["actions"]:
                _registry.control(a["device_id"], a["command"], a.get("params", {}))
            reply = f"⏰ 定时任务「{cfg.get('name', task_id)}」已触发：{SCENES[scene].get('desc', scene)}"
    elif cfg.get("action_type") == "command":
        device_id = cfg.get("device_id")
        command = cfg.get("command")
        params = cfg.get("params", {})
        if isinstance(command, dict):
            command = command.get("command", "")
        if device_id and command:
            _registry.control(device_id, command, params)
            reply = f"⏰ 定时任务「{cfg.get('name', task_id)}」已执行"
    if reply and _manager:
        await _manager.broadcast({
            "type": "agent_reply",
            "reply": reply,
            "digital_human": {"emotion": "happy", "action": "speak"},
            "actions": [],
            "decision_trace": [],
            "requires_confirmation": False,
            "pending_id": "",
            "pending_action": None,
        })


def add_internal_job(job_id: str, func, hour: int, minute: int,
                     timezone: str = "Asia/Shanghai"):
    """注册一个**内部**作业（如 22:00 家庭日报）。

    与 `add_task` 的区别：内部作业不进 `_tasks`、不写 `data/scheduled_tasks.json`、
    不出现在 `/api/schedule/tasks` 里。理由：它是系统功能的一部分，不是用户建的
    定时任务 —— 混在一起的话，用户会在"定时任务"面板里看到一个删不掉的
    "家庭日报"，删了还会因为持久化文件而复活。
    """
    if _scheduler is None:
        raise RuntimeError("调度器未初始化（先调用 init_scheduler）")
    _scheduler.add_job(
        func,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
        id=f"internal:{job_id}",
        replace_existing=True,
        misfire_grace_time=3600,   # 机器休眠/重启错过触发点时，1 小时内仍补跑
    )
    return f"internal:{job_id}"


def list_internal_jobs() -> list[dict]:
    if _scheduler is None:
        return []
    return [
        {"id": j.id, "next_run": getattr(j, "next_run_time", None).isoformat()
         if getattr(j, "next_run_time", None) else None}
        for j in _scheduler.get_jobs() if j.id.startswith("internal:")
    ]


def add_task(cfg: dict) -> str:
    """添加定时任务。cfg: {name, hour, minute, action_type, scene/device_id/command}"""
    import uuid
    task_id = cfg.get("id") or f"task_{uuid.uuid4().hex[:8]}"
    cfg["id"] = task_id
    cfg["created_at"] = datetime.now().isoformat()
    cfg["enabled"] = cfg.get("enabled", True)
    if cfg["enabled"]:
        _add_job(task_id, cfg)
    _tasks[task_id] = cfg
    _save_tasks()
    return task_id


def list_tasks() -> list:
    return list(_tasks.values())


def remove_task(task_id: str) -> bool:
    if task_id in _tasks:
        try:
            _scheduler.remove_job(task_id)
        except Exception:
            pass
        del _tasks[task_id]
        _save_tasks()
        return True
    return False


def toggle_task(task_id: str, enabled: bool) -> bool:
    if task_id in _tasks:
        _tasks[task_id]["enabled"] = enabled
        if enabled:
            _add_job(task_id, _tasks[task_id])
        else:
            try:
                _scheduler.remove_job(task_id)
            except Exception:
                pass
        _save_tasks()
        return True
    return False
