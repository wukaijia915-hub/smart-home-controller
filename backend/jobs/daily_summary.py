# -*- coding: utf-8 -*-
"""外环：22:00 家庭日报（LLM 优先，模板降级）。

数据来源是三份**真实存在的记录**，不是语言模型自由发挥：

| 来源 | 取什么 |
|---|---|
| `StateStore.audit_log` | 今天执行了哪些动作、成功几次、失败几次 |
| `SensorSeries`（本模块，内环每 30s 采样） | 温湿度/空气/光照的最低-平均-最高 |
| `memory/habits` + `experience` | 最常用场景、最常操作设备、偏好冲突、成功案例数 |
| 感知层最近一次评估 | 情境区域标签 |

**LLM 在这里的角色是"把事实讲成人话"，不是"编故事"。** 因此 prompt 里
明确要求"只依据给定数据，不要编造任何数字"，且返回体里 `source` 字段如实标注
这次是 `llm` 还是 `template` —— 断网演练时用户看到的是模板文案，系统**不假装**
自己用了大模型。

移植自心镜的 `aigc/report_generator.py` 的「LLM 优先 + 模板降级」控制流
（它的领域是学生心理日报，我们只搬控制流，不搬领域内容与模板文案）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ..config_loader import data_path, load_loops

REPORT_PATH = data_path("daily_summary.jsonl")
LATEST_PATH = data_path("daily_summary_latest.json")

# 传感器采样：内环 2s 一跳，这里每 15 跳（约 30s）记一次，24 小时约 2880 点。
# 直接按 2s 全存的话一天 43200 点，纯属浪费；日报要的是量级不是波形。
SAMPLE_EVERY_N_TICKS = 15
MAX_SAMPLES = 2880
DEFAULT_WINDOW_HOURS = 24.0


class SensorSeries:
    """环形缓冲的传感器时序。日报的"最低/平均/最高"必须来自真实采样。

    不给日报传当前读数冒充"全天统计" —— 那是伪造数据，且很容易被问穿
    （"你说的最高 31℃ 是什么时候测到的？"）。
    """

    def __init__(self, every_n: int = SAMPLE_EVERY_N_TICKS, max_samples: int = MAX_SAMPLES):
        self.every_n = max(1, every_n)
        self.max_samples = max_samples
        self._tick = 0
        self._samples: list[tuple[float, dict]] = []

    def tick(self, sensors: dict) -> bool:
        """每次传感器心跳调用一次；每 N 跳真正记一次。返回本次是否记录。"""
        self._tick += 1
        if self._tick % self.every_n:
            return False
        self.sample(sensors)
        return True

    def sample(self, sensors: dict, ts: float | None = None) -> None:
        keys = ("temp_indoor", "humidity", "light_level", "air_quality")
        snapshot = {k: sensors[k] for k in keys if isinstance(sensors.get(k), (int, float))}
        if not snapshot:
            return
        self._samples.append((time.time() if ts is None else ts, snapshot))
        del self._samples[:-self.max_samples]

    def summary(self, since: float | None = None) -> dict:
        since = 0.0 if since is None else since
        rows = [s for ts, s in self._samples if ts >= since]
        if not rows:
            return {"samples": 0, "note": "本次窗口内没有采样（服务刚启动或采样间隔未到）"}
        out = {"samples": len(rows)}
        for key in ("temp_indoor", "humidity", "light_level", "air_quality"):
            vals = [r[key] for r in rows if key in r]
            if vals:
                out[key] = {
                    "min": round(min(vals), 1),
                    "max": round(max(vals), 1),
                    "avg": round(sum(vals) / len(vals), 1),
                }
        return out

    def reset(self) -> None:
        self._samples.clear()
        self._tick = 0


series = SensorSeries()


# ---------------------------------------------------------------- 事实采集
def collect_facts(registry, store, perception_state: dict | None = None,
                  window_hours: float = DEFAULT_WINDOW_HOURS,
                  habits_store=None) -> dict:
    """从审计日志 + 传感器时序 + 习惯统计汇总今日事实。不含任何推断。"""
    from ..memory import habits as habits_mod

    hab = habits_store or habits_mod.habits
    now = time.time()
    since = now - window_hours * 3600.0

    audit_all = store.get_audit(limit=10_000)
    entries = store.audit_since(since)
    ok = [e for e in entries if e.get("ok")]
    failed = [e for e in entries if not e.get("ok")]

    by_device: dict[str, int] = {}
    by_room: dict[str, int] = {}
    for e in entries:
        did = e.get("device_id", "")
        by_device[did] = by_device.get(did, 0) + 1
        device = registry.get(did)
        room = getattr(device, "room", "") if device else ""
        if room:
            by_room[room] = by_room.get(room, 0) + 1

    name_of = lambda did: (getattr(registry.get(did), "name", did) or did)  # noqa: E731
    habit_stats = hab.stats(name_of=name_of)

    top_device = max(by_device.items(), key=lambda kv: kv[1]) if by_device else None
    facts = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_hours": window_hours,
        "actions": {
            "total": len(entries),
            "ok": len(ok),
            "failed": len(failed),
            "audit_buffer": len(audit_all),
            # 审计日志是环形的，超出缓冲的旧记录会被丢弃 —— 这决定了
            # "今日动作数"上限是缓冲大小，必须如实说明，否则数字会被误解为真实总数
            "note": f"审计日志为环形缓冲（上限 {store.max_log} 条），"
                    f"窗口内实际读到的条数受此限制",
        },
        "top_devices": [
            {"device_id": d, "name": name_of(d), "count": c}
            for d, c in sorted(by_device.items(), key=lambda kv: kv[1], reverse=True)[:5]
        ],
        "top_rooms": [
            {"room": r, "count": c}
            for r, c in sorted(by_room.items(), key=lambda kv: kv[1], reverse=True)[:5]
        ],
        "top_device": ({"device_id": top_device[0], "name": name_of(top_device[0]),
                        "count": top_device[1]} if top_device else None),
        "failed_actions": [
            {"device_id": e.get("device_id"), "command": e.get("command"),
             "name": name_of(e.get("device_id", ""))} for e in failed[:5]
        ],
        "sensors": series.summary(since=since),
        "habits": {
            "top_scene": habit_stats.get("top_scene"),
            "scene_counts": hab.data.get("scenes", {}),
            "declared_preferences": habit_stats.get("declared_preferences", {}),
            "conflicts": habit_stats.get("conflicts", []),
            "total_commands": habit_stats.get("total_commands", 0),
        },
        "situation": _situation_brief(perception_state),
        "device_health": [
            {"device_id": d.device_id, "name": d.name, "state": d.state}
            for d in registry.all() if d.state in ("offline", "fault")
        ],
    }
    return facts


def _situation_brief(perception_state: dict | None) -> dict:
    if not perception_state:
        return {"available": False}
    sit = perception_state.get("situation") or {}
    return {
        "available": True,
        "region": sit.get("region"),
        "region_label": sit.get("region_label"),
        "region_level": sit.get("region_level"),
    }


# ---------------------------------------------------------------- 模板降级
def render_template(facts: dict) -> str:
    """"LLM 优先 / 模板降级"的降级侧。没有 Key 或调用失败时用它。

    模板也必须是**真有信息量**的：降级文案要是只有一句"今天一切正常"，
    那这个功能在没有 Key 的环境下就等于不存在，而"断网也能演示"正是本项目
    的卖点之一。
    """
    parts = []
    acts = facts.get("actions", {})
    total = acts.get("total", 0)
    if total == 0:
        parts.append("今天家里没有记录到设备操作，很安静。")
    else:
        scene = facts.get("habits", {}).get("top_scene")
        scene_txt = f"，最常进入的是「{scene}」模式" if scene else ""
        parts.append(f"今天一共执行了 {total} 次设备操作"
                     f"（成功 {acts.get('ok', 0)} 次，失败 {acts.get('failed', 0)} 次）{scene_txt}。")

    top = facts.get("top_device")
    if top:
        parts.append(f"操作最多的是「{top['name']}」（{top['count']} 次）。")

    rooms = facts.get("top_rooms") or []
    if len(rooms) > 1:
        parts.append("活动集中在" + "、".join(f"{r['room']}（{r['count']} 次）" for r in rooms[:3]) + "。")

    sensors = facts.get("sensors", {})
    temp = sensors.get("temp_indoor")
    if temp:
        parts.append(f"室温在 {temp['min']:.0f}~{temp['max']:.0f}℃ 之间，平均 {temp['avg']:.0f}℃"
                     f"（{sensors.get('samples', 0)} 个采样点）。")
    air = sensors.get("air_quality")
    if air:
        parts.append(f"空气质量平均 {air['avg']:.0f}。")

    sit = facts.get("situation", {})
    if sit.get("available") and sit.get("region_label"):
        parts.append(f"当前家庭情境判定为「{sit['region_label']}」。")

    conflicts = facts.get("habits", {}).get("conflicts") or []
    if conflicts:
        parts.append(conflicts[0]["question"])

    failed = facts.get("failed_actions") or []
    if failed:
        names = "、".join(f["name"] or f["device_id"] for f in failed[:3])
        parts.append(f"有 {acts.get('failed', 0)} 次操作未成功（涉及 {names}），建议检查这些设备。")
    unhealthy = facts.get("device_health") or []
    if unhealthy:
        names = "、".join(f"{d['name']}（{'离线' if d['state'] == 'offline' else '故障'}）"
                          for d in unhealthy[:3])
        parts.append(f"设备状态需要注意：{names}。")
    if total and not failed and not unhealthy:
        parts.append("设备都正常工作，没有异常需要处理。")
    return "".join(parts)


# ---------------------------------------------------------------- LLM 侧
def _build_prompt(facts: dict) -> list[dict]:
    return [
        {"role": "system", "content":
            "你是家庭智能中枢「灵犀」，负责在每天固定时间给住户写一段简短的当日报小结。\n"
            "硬性要求：\n"
            "1. 只依据下面给出的数据写，**不得编造任何数字或事件**。数据里没有的就说没有。\n"
            "2. 3~5 句话，中文，口语化，称呼住户为「您」。\n"
            "3. 顺序：今天家里发生了什么 → 有没有需要留意的 → 一条具体可执行的建议。\n"
            "4. 不要罗列 JSON 字段名，不要用 Markdown 标题或列表符号，直接写成一段话。\n"
            "5. 如果数据很少（几乎没有操作），就简短诚实地说今天家里很安静，不要硬凑内容。"},
        {"role": "user", "content":
            "以下是系统真实采集到的数据（JSON）：\n"
            + json.dumps(facts, ensure_ascii=False, indent=1)
            + "\n\n请生成今日家庭小结。"},
    ]


def generate(llm, facts: dict) -> dict:
    """LLM 优先、模板降级。返回 {narrative, source, llm_ok, degrade_reason}。"""
    cfg = (load_loops().get("outer_loop") or {})
    max_tokens = int(cfg.get("llm_max_tokens", 700))
    timeout = float(cfg.get("llm_timeout_seconds", 20.0))

    # 降级原因要能区分**三种完全不同的情况**：没配 Key、处于离线模式、
    # 配了但调用失败。混成一句"模型不可用"的话，演示现场就分不清
    # "断网演练本来就没打算调模型"和"模型挂了"，而这恰恰是最需要一眼看清的事。
    narrative, reason = None, ""
    if llm is None:
        reason = "未装配 LLM 客户端（本次调用未传 llm）"
    elif not getattr(llm, "available", False):
        offline = getattr(llm, "offline", False)
        reason = ("LLM 处于离线模式（LLM_OFFLINE=1），按设计不发起网络请求"
                  if offline else "LLM 不可用：未配置任何 API Key")
    else:
        try:
            narrative = llm.chat_text(
                _build_prompt(facts), temperature=0.4,
                max_tokens=max_tokens, context_text="家庭日报",
                timeout=timeout)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        if not narrative:
            reason = reason or "模型未返回文本（调用成功但内容为空）"

    if narrative:
        return {"narrative": narrative, "source": "llm", "llm_ok": True,
                "degrade_reason": "", "backend": getattr(llm, "last_used_backend", None)}
    return {"narrative": render_template(facts), "source": "template", "llm_ok": False,
            "degrade_reason": reason or "模型不可用（原因未知）", "backend": None}


# ---------------------------------------------------------------- 落盘 / 入口
def run(registry, store, llm, perception_state: dict | None = None,
        broadcast=None, window_hours: float = DEFAULT_WINDOW_HOURS,
        habits_store=None) -> dict:
    """生成一份日报：采集事实 → LLM 或模板 → 落盘 → （可选）广播。

    任何一步失败都不抛异常：外环的失败不该影响正在运行的内环。
    """
    start = time.perf_counter()
    try:
        facts = collect_facts(registry, store, perception_state,
                              window_hours=window_hours, habits_store=habits_store)
        result = generate(llm, facts)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    report = {
        "ok": True,
        "date": facts["date"],
        "generated_at": facts["generated_at"],
        "window_hours": window_hours,
        "source": result["source"],
        "narrative": result["narrative"],
        "facts": facts,
        "llm": {"ok": result["llm_ok"], "backend": result["backend"],
                "degrade_reason": result["degrade_reason"]},
        "duration_ms": round((time.perf_counter() - start) * 1000, 1),
    }
    _persist(report)
    if broadcast is not None:
        try:
            broadcast(report)
        except Exception:
            pass
    return report


def _persist(report: dict) -> None:
    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(REPORT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
        LATEST_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        print(f"[外环·日报] 落盘失败: {e}")


def latest() -> dict | None:
    if not LATEST_PATH.exists():
        return None
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def history(limit: int = 10) -> list[dict]:
    if not REPORT_PATH.exists():
        return []
    try:
        lines = REPORT_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def stats() -> dict:
    rows = history(limit=1000)
    return {
        "total_reports": len(rows),
        "llm_reports": sum(1 for r in rows if r.get("source") == "llm"),
        "template_reports": sum(1 for r in rows if r.get("source") == "template"),
        "last_at": rows[-1].get("generated_at") if rows else None,
        "sensor_samples": len(series._samples),
        "sample_interval_seconds": SAMPLE_EVERY_N_TICKS * 2,
    }
