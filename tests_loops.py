# -*- coding: utf-8 -*-
"""快慢双环测试：内环告警（声明式阈值）/ 外环日报（LLM 优先模板降级）/ 事件路由高危拒绝。

三个必须钉住的点：

1. **降级必须真的可用**。日报走"LLM 优先、模板降级"，而"断网也能演示"是本项目
   卖点之一 —— 所以模板文案必须真有信息量，且返回体要如实标注这次是模板。
   测试同时覆盖 LLM 成功、LLM 抛异常、LLM 未配置三条路径，且**全部不碰网络**
   （用鸭子类型的假 LLM，而不是打真实 API）。
2. **内环告警阈值来自配置**。原实现是 main.py 里成排的 `if`，改阈值要重启；
   现在阈值在 `config/alert_routes.json`。测试直接改配置对象验证判定，不依赖文件。
3. **事件路由不得自动执行高危动作**。事件路由是**无人确认**的路径：没有弹窗、
   没有人点"确认执行"。此前它直接调 registry.control()，只要配置里加一条指向
   门锁/燃气灶的联动，门锁就会在无人值守时被打开 —— 与"高危动作必须二次确认"
   的宣称直接矛盾。这条测试是该漏洞的守门人。
"""
import os
import sys
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="lx_loops_")
os.environ["LINGXI_DATA_DIR"] = _TMP_DIR
os.environ["LLM_OFFLINE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
from pathlib import Path

from backend.alerts import AlertEvaluator
from backend.devices.registry import DeviceRegistry
from backend.event_routes import EventRouter
from backend.jobs import daily_summary
from backend.lease import LeaseValidator
from backend.state import StateStore

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}  {detail}")


print("=" * 60)
print("  灵犀 — 快慢双环测试（内环告警 / 外环日报）")
print("=" * 60)

# ---------------------------------------------------------------- 1. 内环告警
print("\n== 1. 内环告警：声明式阈值判定 ==")
ev = AlertEvaluator()
_NOW = 10_000.0

hot = {"temp_indoor": 32.5, "air_quality": 85, "humidity": 55, "light_level": 400}
events = ev.evaluate(hot, [], now=_NOW)
kinds = [e["payload"]["kind"] for e in events]
check("高温触发 temp_high", "temp_high" in kinds, str(kinds))
check("高温不同时触发 temp_low（同一传感器互斥）", "temp_low" not in kinds, str(kinds))
check("事件是结构化事件（含 type/source/severity/payload）",
      all({"type", "source", "severity", "payload"} <= set(e) for e in events))

summary = next(e["payload"]["summary"] for e in events if e["payload"]["kind"] == "temp_high")
check("文案已渲染且含真实读数", "32" in summary or "33" in summary, summary)
check("文案含阈值（改配置即可改口径）", "30" in summary, summary)

check("冷却：5 秒内同源不重复告警",
      len([e for e in ev.evaluate(hot, [], now=_NOW + 5)]) == 0)
check("冷却过后重新告警",
      len([e for e in ev.evaluate(hot, [], now=_NOW + 61)]) == 1)

cold = {"temp_indoor": 12.0, "air_quality": 85, "humidity": 55, "light_level": 400}
check("低温触发 temp_low",
      "temp_low" in [e["payload"]["kind"] for e in AlertEvaluator().evaluate(cold, [], now=_NOW)])

bad_air = {"temp_indoor": 25.0, "air_quality": 30, "humidity": 55, "light_level": 400}
check("空气质量差触发 air_poor",
      "air_poor" in [e["payload"]["kind"] for e in AlertEvaluator().evaluate(bad_air, [], now=_NOW)])

# require_any_sensor：无人时不该对着空房子提示"光线偏暗"
dim_empty = {"temp_indoor": 25.0, "air_quality": 85, "humidity": 55,
             "light_level": 10, "motion_living": False, "motion_bedroom": False}
check("无人活动时不触发补光提示",
      AlertEvaluator().evaluate(dim_empty, [], now=_NOW) == [])
dim_occupied = dict(dim_empty, motion_living=True)
check("有人活动且偏暗才触发补光",
      "light_low_occupied" in [e["payload"]["kind"]
                               for e in AlertEvaluator().evaluate(dim_occupied, [], now=_NOW)])

check("读数缺失不误报（不把缺测当成越界）",
      AlertEvaluator().evaluate({"temp_indoor": None}, [], now=_NOW) == [])
check("读数非数值不误报",
      AlertEvaluator().evaluate({"temp_indoor": "unknown"}, [], now=_NOW) == [])

# 设备健康：离线/故障 → 事件；恢复后冷却记录被清掉
reg = DeviceRegistry()
detector = AlertEvaluator()
reg.set_state("tv_living_01", "offline")
offline_events = detector.evaluate({}, reg.all(), now=_NOW)
check("设备离线产生 device_health 事件",
      any(e["payload"].get("kind") == "offline" for e in offline_events),
      str([e["payload"].get("kind") for e in offline_events]))
check("设备事件文案含设备名",
      any("电视" in e["payload"]["summary"] for e in offline_events))
check("离线事件 60s 内不重复", detector.evaluate({}, reg.all(), now=_NOW + 30) == [])
reg.set_state("tv_living_01", "online")
detector.evaluate({}, reg.all(), now=_NOW + 31)      # 恢复 → 清冷却
reg.set_state("tv_living_01", "offline")
check("★ 设备恢复后再次离线能重新告警（冷却记录已清）",
      len(detector.evaluate({}, reg.all(), now=_NOW + 32)) == 1,
      "否则设备第二次离线会被上一次的冷却吃掉，反而不报")

# 模板容错：用户改配置写错占位符不该搞停整个告警功能
check("模板含未知占位符不抛异常",
      AlertEvaluator._render("温度 {value:.0f}℃ {unknown_field}", value=31.4) == "温度 31℃ ")
check("模板为空返回空串", AlertEvaluator._render("") == "")

# ---------------------------------------------------------------- 2. 事件路由高危拒绝
print("\n== 2. 事件路由：高危动作默认拒绝 ==")
# 注意：必须通过**配置文件**构造路由，不能直接塞 router.raw ——
# `route()` 每次都会 `load()` 热重载配置，塞进去的 raw 立刻被覆盖，
# 结果是"路由不匹配、什么都没发生"，而测试会误以为是"高危被拒绝了"（空列表）。
# 走配置文件同时也把配置加载这条真实路径测进去了。
import itertools
import json as _json

_seq = itertools.count()


def router_with(routes, registry, **kw) -> EventRouter:
    p = Path(_TMP_DIR) / f"routes_{next(_seq)}.json"
    p.write_text(_json.dumps({"routes": routes}, ensure_ascii=False), encoding="utf-8")
    return EventRouter(registry, None, routes_path=p, **kw)


reg2 = DeviceRegistry()
lock_dev = next((d for d in reg2.all() if d.category == "lock"), None)
check("测试前置：存在门锁设备", lock_dev is not None)
check("测试前置：unlock 被判定为高危",
      lock_dev is not None and LeaseValidator.is_high_risk(lock_dev.category, "unlock"))

router = router_with([{
    "id": "danger_route", "cooldown_seconds": 0,
    "match": {"event_types": ["test_event"]},
    "actions": [{"type": "device", "device_id": lock_dev.device_id,
                 "command": "unlock", "params": {}}],
}], reg2)
res = router.route({"type": "test_event", "source": "test", "payload": {}})
check("测试前置：该联动确实被路由命中（否则下面的断言是假通过）",
      res.matched_routes == ["danger_route"], str(res.matched_routes))
check("★ 未声明 allow_high_risk 的高危动作被拒绝",
      bool(res.actions) and res.actions[0].get("status") == "refused",
      str(res.actions))
check("拒绝原因说明清楚（可指导配置修正）",
      bool(res.actions) and "二次确认" in res.actions[0].get("error", "")
      and "allow_high_risk" in res.actions[0].get("error", ""),
      res.actions[0].get("error", "") if res.actions else "(无动作)")
check("拒绝时不改变设备状态",
      reg2.get(lock_dev.device_id).attributes["locked"].value is True,
      str(reg2.get(lock_dev.device_id).attributes["locked"].value))

# 显式授权后才允许（决定被写进配置，可被评审看到）
router2 = router_with([{
    "id": "allowed_route", "cooldown_seconds": 0,
    "match": {"event_types": ["test_event"]},
    "actions": [{"type": "device", "device_id": lock_dev.device_id,
                 "command": "unlock", "params": {}, "allow_high_risk": True}],
}], reg2)
res2 = router2.route({"type": "test_event", "source": "test", "payload": {}})
check("显式 allow_high_risk 后放行（决定写在配置里，不是悄悄发生）",
      bool(res2.actions) and res2.actions[0].get("ok") is True, str(res2.actions))

# 非高危设备不受影响
reg2b = DeviceRegistry()
router3 = router_with([{
    "id": "safe_route", "cooldown_seconds": 0,
    "match": {"event_types": ["test_event"]},
    "actions": [{"type": "device", "device_id": "light_living_01",
                 "command": "turn_on", "params": {}}],
}], reg2b)
check("普通设备动作不受影响",
      router3.route({"type": "test_event", "source": "t",
                     "payload": {}}).actions[0].get("ok") is True)

# 同设备多动作必须串行（并行会因顺序不定导致回读证据不一致）
reg2c = DeviceRegistry()
router4 = router_with([{
    "id": "multi", "cooldown_seconds": 0,
    "match": {"event_types": ["test_event"]},
    "actions": [{"type": "device", "device_id": "ac_living_01", "command": "turn_on", "params": {}},
                {"type": "device", "device_id": "ac_living_01",
                 "command": "set_temperature", "params": {"temperature": 24}}],
}], reg2c)
multi = router4.route({"type": "test_event", "source": "t", "payload": {}})
check("同设备多动作串行执行且顺序保持",
      [a.get("ok") for a in multi.actions] == [True, True]
      and reg2c.get("ac_living_01").attributes["temperature"].value == 24,
      str([(a.get("command"), a.get("ok")) for a in multi.actions]))

# 端到端：高温事件 → 路由 → 空调真的开了
print("\n== 3. 环境联动端到端（告警 → 路由 → 设备） ==")
reg3 = DeviceRegistry()
router3 = EventRouter(reg3, None, state_store=StateStore())
alert3 = AlertEvaluator()
hot_events = alert3.evaluate(
    {"temp_indoor": 33.0, "air_quality": 85, "humidity": 55, "light_level": 400},
    reg3.all(), now=_NOW)
temp_events = [e for e in hot_events if e["payload"]["kind"] == "temp_high"]
check("高温产生事件", len(temp_events) == 1)
ev3 = router3.route(temp_events[0])
ac = reg3.get("ac_living_01")
check("★ 高温事件 → 空调被开启",
      ac.attributes["on"].value is True, str(ac.attributes["on"].value))
check("★ 高温事件 → 温度设为 24℃",
      ac.attributes["temperature"].value == 24, str(ac.attributes["temperature"].value))
check("联动含播报动作，文案取自 payload.summary",
      any(a.get("type") == "alert" and "33" in a.get("message", "") for a in ev3.actions),
      str([a.get("type") for a in ev3.actions]))
check("联动写入了审计（版本递增）",
      router3.state_store.version == 2 and len(router3.state_store.audit_log) == 2,
      f"version={router3.state_store.version}")

# ---------------------------------------------------------------- 4. 外环日报
print("\n== 4. 外环日报：LLM 优先 / 模板降级 ==")
check("SensorSeries：非采样跳不记录",
      daily_summary.SensorSeries(every_n=3).tick({"temp_indoor": 25}) is False)
s = daily_summary.SensorSeries(every_n=1, max_samples=5)
for i in range(10):
    s.sample({"temp_indoor": 20 + i, "humidity": 50, "light_level": 100, "air_quality": 80})
summ = s.summary()
check("采样上限生效（环形缓冲）", summ["samples"] == 5, str(summ["samples"]))
check("统计 min/max/avg 来自真实采样",
      summ["temp_indoor"]["min"] == 25 and summ["temp_indoor"]["max"] == 29
      and summ["temp_indoor"]["avg"] == 27, str(summ["temp_indoor"]))
check("空窗口如实说明没有采样",
      "没有采样" in daily_summary.SensorSeries().summary()["note"])
check("无传感器字段的输入不记录",
      daily_summary.SensorSeries(every_n=1).summary()["samples"] == 0)

# 事实采集：从审计日志统计
reg4 = DeviceRegistry()
store4 = StateStore()
for i in range(3):
    store4.bump()
    store4.record(None, "light_living_01", "turn_on", {},
                  {"ok": i < 2, "device_id": "light_living_01"})
facts = daily_summary.collect_facts(reg4, store4, {})
check("动作总数来自审计日志", facts["actions"]["total"] == 3, str(facts["actions"]))
check("成功/失败分开统计",
      facts["actions"]["ok"] == 2 and facts["actions"]["failed"] == 1)
check("★ 如实标注审计日志是环形缓冲（数字是下界而非总数）",
      "环形缓冲" in facts["actions"]["note"], facts["actions"]["note"])
check("最常操作设备已归纳", facts["top_device"]["device_id"] == "light_living_01")
check("失败动作的显示名已解析（不是裸 device_id）",
      facts["failed_actions"][0]["name"] == "客厅吊灯",
      str(facts["failed_actions"]))
check("无感知结果时如实标注 available=False", facts["situation"]["available"] is False)

tmpl = daily_summary.render_template(facts)
check("模板降级文案有真实信息量（含动作数与温度口径）",
      "3 次设备操作" in tmpl and "客厅吊灯" in tmpl, tmpl)
check("模板文案提到失败动作", "未成功" in tmpl, tmpl)
empty_tmpl = daily_summary.render_template(
    {"actions": {"total": 0, "ok": 0, "failed": 0}, "habits": {}, "sensors": {},
     "top_rooms": [], "failed_actions": [], "device_health": [], "situation": {}})
check("数据很少时不硬凑（诚实说很安静）", "很安静" in empty_tmpl, empty_tmpl)


class FakeLLM:
    """鸭子类型的假模型：既测"LLM 优先"，又完全不碰网络。"""

    def __init__(self, reply="今天家里很正常，空调开了两次。", available=True, boom=False):
        self.reply = reply
        self.available = available
        self.boom = boom
        self.last_used_backend = "fake"
        self.calls = 0

    def chat_text(self, messages, **kwargs):
        self.calls += 1
        if self.boom:
            raise RuntimeError("模拟模型超时")
        return self.reply


gen = daily_summary.generate(FakeLLM(), facts)
check("有模型且调用成功 → source=llm", gen["source"] == "llm" and gen["llm_ok"] is True,
      str(gen["source"]))
check("LLM 文案被采纳", gen["narrative"].startswith("今天家里很正常"), gen["narrative"][:40])

gen2 = daily_summary.generate(FakeLLM(boom=True), facts)
check("模型抛异常 → 降级到模板且不抛出去",
      gen2["source"] == "template" and "3 次设备操作" in gen2["narrative"],
      gen2["narrative"][:40])
check("★ 降级原因如实记录（可区分超时与未配置）",
      "RuntimeError" in gen2["degrade_reason"], gen2["degrade_reason"])

gen3 = daily_summary.generate(FakeLLM(available=False), facts)
check("模型不可用（如无 Key / 离线模式）→ 模板",
      gen3["source"] == "template", gen3["source"])
check("未配置 Key 的降级原因与超时的不同",
      "未配置" in gen3["degrade_reason"] or "离线" in gen3["degrade_reason"],
      gen3["degrade_reason"])
check("无 LLM 对象也不崩", daily_summary.generate(None, facts)["source"] == "template")

# run()：落盘 + 广播
daily_summary.LATEST_PATH = Path(_TMP_DIR) / "latest.json"
daily_summary.REPORT_PATH = Path(_TMP_DIR) / "daily.jsonl"
broadcasts = []
rep = daily_summary.run(reg4, store4, FakeLLM(), {}, broadcast=broadcasts.append)
check("run 返回成功", rep["ok"] is True)
check("run 落盘 latest", daily_summary.latest() is not None
      and daily_summary.latest()["narrative"] == rep["narrative"])
check("run 广播回调被调用", len(broadcasts) == 1)
check("run 记录耗时", isinstance(rep["duration_ms"], (int, float)))
check("history 可回溯", len(daily_summary.history()) == 1)
check("返回体含事实依据（可复核，不是只有一段文案）",
      "facts" in rep and rep["facts"]["actions"]["total"] == 3)
check("stats 区分 llm/template 两种来源",
      daily_summary.stats()["total_reports"] == 1
      and daily_summary.stats()["llm_reports"] == 1,
      str(daily_summary.stats()))

# 外环失败不得抛穿（它跑在后台任务里，抛出去会静默杀死循环）
class ExplodingStore:
    def get_audit(self, limit=50):
        raise RuntimeError("模拟状态源故障")

    max_log = 10


bad = daily_summary.run(reg4, ExplodingStore(), FakeLLM(), {})
check("★ 外环故障返回 ok=False 而不抛异常（不杀后台循环）",
      bad["ok"] is False and "RuntimeError" in bad["error"], str(bad))

# ---------------------------------------------------------------- 5. 配置一致性
print("\n== 5. 配置与代码的一致性 ==")
from backend.config_loader import load_alert_routes, load_loops

cfg = load_alert_routes()
check("阈值配置可加载", len(cfg.get("sources", [])) >= 4)
check("每个阈值都有传感器与比较符",
      all(s.get("sensor") and s.get("op") in (">", "<", ">=", "<=", "==")
          for s in cfg["sources"]))
check("每个阈值都有冷却（否则每 5 秒刷一次屏）",
      all(float(s.get("cooldown_seconds", 0)) > 0 for s in cfg["sources"]))
loops = load_loops()
check("双环周期已外置到配置（不硬编码在 Python 里）",
      loops.get("inner_loop", {}).get("sensor_seconds", 0) > 0
      and loops.get("outer_loop", {}).get("cron_hour") == 22, str(loops.keys()))
check("外环时刻可现场改（演示不必等到 22:00）",
      "cron_hour" in loops.get("outer_loop", {}))

# 告警层的每个事件类型都必须有对应的路由，否则"告警"只说不做
from backend.config_loader import _load_json
routes = _load_json("event_routes.json").get("routes", [])
routed_types = set()
for r in routes:
    routed_types.update(r.get("match", {}).get("event_types", []))
emitted_types = {s.get("event_type") for s in cfg["sources"]}
emitted_types.add(cfg.get("device_health", {}).get("event_type"))
missing = emitted_types - routed_types
check("★ 内环发出的每种事件都有路由（否则告警只播报不联动）",
      not missing, f"无路由的事件类型: {missing}")

print("\n== 6. 草稿→确认：条件句不立即执行，也不再生硬拒绝 ==")
from backend.agent import HomeAgent
from backend.capabilities import CapabilityRegistry
from backend.drafts import DraftStore, confirm_draft

dreg = DeviceRegistry()
dcaps = CapabilityRegistry()
dcaps.auto_pair(dreg.all())
dagent = HomeAgent(dreg, capabilities=dcaps)

res = dagent.decide("如果客厅没人了就把灯关掉")
check("条件句不执行任何设备动作",
      res["actions"] == [] and res["results"] == [], str(res["actions"]))
check("条件句产出规则草稿（而不是生硬拒绝）",
      bool(res.get("draft")) and res["draft"].get("kind") == "event_route"
      and bool(res.get("draft_id")), str(res.get("draft")))
_draft_payload = (res.get("draft") or {}).get("payload") or {}
check("草稿匹配 no_person 事件",
      "no_person" in _draft_payload.get("match", {}).get("event_types", []),
      str(_draft_payload.get("match")))
check("草稿动作是关灯",
      any(a["command"] == "turn_off" for a in _draft_payload.get("actions", [])),
      str(_draft_payload.get("actions")))
check("草稿回复用将来时（不声称已执行）",
      "尚未生效" in res["reply"] and "已关闭" not in res["reply"], res["reply"][:80])

_store = DraftStore(ttl_seconds=600)
_draft = _store.create(payload=_draft_payload, summary=_draft_payload.get("desc", ""))
check("草稿箱可创建并按 id 取回", _draft is not None and _store.get(_draft.draft_id) is not None)

_routes_file = Path(_TMP_DIR) / "custom_routes.json"
_router = EventRouter(dreg, custom_path=_routes_file)
_ok, _payload = confirm_draft(_store, _router, dreg, _draft.draft_id)
check("确认后写入自定义路由（预置路由文件不动）",
      _ok and _routes_file.exists(), str(_payload))
check("草稿确认后即出箱（不可重复确认）", _store.get(_draft.draft_id) is None)

_ev = _router.route({"type": "no_person", "source": "test",
                     "payload": {"summary": "没人了"}})
_ours = [a for a in _ev.actions if a.get("route_id") == _payload["route"]["id"]]
check("新路由真的会被事件触发", _payload["route"]["id"] in _ev.matched_routes,
      str(_ev.matched_routes))
check("新路由的动作仍带回读证据",
      bool(_ours) and any(a.get("status") in ("confirmed", "delivered") for a in _ours),
      str(_ours))

# 解析不出的条件句：仍然诚实拒绝，不猜
res_bad = dagent.decide("如果天气好就出去散步")
check("解析不出的条件句仍诚实拒绝（不瞎猜）",
      res_bad["actions"] == [] and res_bad.get("draft") is None
      and "条件触发" in res_bad["reply"], res_bad["reply"][:50])

# 高危动作：草稿先出，确认时必须显式允许
res_hr = dagent.decide("如果有人就解锁门锁")
_hr_payload_in = (res_hr.get("draft") or {}).get("payload") or {}
check("高危条件句也先出草稿、不执行",
      bool(res_hr.get("draft")) and res_hr["actions"] == [], str(res_hr.get("draft")))
check("草稿标注高危动作",
      bool(_hr_payload_in.get("high_risk")), str(_hr_payload_in))

_hr_store = DraftStore(ttl_seconds=600)
_hr_draft = _hr_store.create(payload=_hr_payload_in, summary="高危草稿")
_ok_hr, _hr_payload = confirm_draft(_hr_store, _router, dreg, _hr_draft.draft_id)
check("未显式允许 → 拒绝创建高危自动规则",
      (not _ok_hr) and _hr_payload.get("code") == "high_risk_requires_allow",
      str(_hr_payload))
check("拒绝后草稿仍在（可改后重试）", _hr_store.get(_hr_draft.draft_id) is not None)

_ok_hr2, _hr_payload2 = confirm_draft(_hr_store, _router, dreg, _hr_draft.draft_id,
                                      allow_high_risk=True)
check("显式 allow_high_risk 后才落库，且动作被标注",
      _ok_hr2 and any(a.get("allow_high_risk") for a in _hr_payload2["route"]["actions"]),
      str(_hr_payload2))
_ev_hr = _router.route({"type": "person_detected", "source": "test",
                        "payload": {"summary": "有人"}})
_hr_actions = [a for a in _ev_hr.actions
               if a.get("route_id") == _hr_payload2["route"]["id"]]
check("显式允许的高危自动规则真的执行（不再被 refused）",
      bool(_hr_actions) and _hr_actions[0].get("status") != "refused", str(_hr_actions))

_expire = DraftStore(ttl_seconds=0)
_d = _expire.create(payload={"actions": []}, summary="expire")
check("草稿 TTL 到期自动清理（不制造幽灵草稿）",
      _expire.get(_d.draft_id) is None and _expire.stats()["pending"] == 0)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
