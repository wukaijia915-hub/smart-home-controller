# -*- coding: utf-8 -*-
"""灵犀 2.0 融合能力演示脚本（python demo_fusion.py）。

按 `docs/答辩Q&A预案.md` §五推荐的顺序走一遍，**10 个演示点**：

  D1  规则层延迟（毫秒级，零 LLM）
  D2  五级漏斗：精确规则优先于宽泛规则
  D3  条件句保护：诚实拒绝，而不是误执行
  D4  高危二次确认：未确认不执行
  D5  分层延迟：规则层与慢环分开显示
  D6  多模态融合：模态冲突 → 整组丢弃
  D7  白盒情境参数：每个参数都能展开原始依据
  D8  记忆召回：慢环规划时注入了哪条历史案例（可复核）
  D9  外环日报：LLM 优先 / 模板降级，且降级可见
  D10 内环告警 → 环境联动（确定性阈值，与感知层性质不同）

**全程离线可跑**（不发起任何网络请求）：本脚本不设置 API Key 也不调用真实模型，
D8 用鸭子类型的假模型驱动慢环，D9 走模板降级路径。
这样做的理由是"断网演练"本身就是一个演示点 —— 演示脚本如果依赖网络，
它就没法演示"不依赖网络"这件事。

用法：
    LLM_OFFLINE=1 python demo_fusion.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")

# 隔离运行时数据：演示不应写进真实用户的习惯/经验库
os.environ.setdefault("LLM_OFFLINE", "1")
os.environ.setdefault("LINGXI_DATA_DIR", tempfile.mkdtemp(prefix="lx_demo_"))

from backend.agent import HomeAgent                                     # noqa: E402
from backend.alerts import AlertEvaluator                               # noqa: E402
from backend.capabilities import CapabilityRegistry                     # noqa: E402
from backend.devices.registry import DeviceRegistry                     # noqa: E402
from backend.event_routes import EventRouter                            # noqa: E402
from backend.jobs import daily_summary                                  # noqa: E402
from backend.memory import experience as experience_mod                 # noqa: E402
from backend.observability.llm_log import llm_log                       # noqa: E402
from backend.perception import PerceptionContext, PerceptionService     # noqa: E402
from backend.state import StateStore                                     # noqa: E402


STEP = {"n": 0}


def hr(title: str):
    STEP["n"] += 1
    print(f"\n{'=' * 70}\n  D{STEP['n']}  {title}\n{'=' * 70}")


def note(text: str):
    print(f"  → {text}")


# ============ 初始化 ============
registry = DeviceRegistry()
caps = CapabilityRegistry()
caps.auto_pair(registry.all())
agent = HomeAgent(registry, capabilities=caps)


class DemoManager:
    async def broadcast(self, message: dict):
        txt = message.get("message") or message.get("reply", "")
        if txt:
            print(f"      [数字人播报] {txt[:70]}")


print("=" * 70)
print("  灵犀 2.0 · 融合能力演示")
print("=" * 70)
print(f"  运行时数据目录：{os.environ['LINGXI_DATA_DIR']}（隔离，不写真实数据）")
print(f"  LLM_OFFLINE={os.environ['LLM_OFFLINE']}（本脚本不发起任何网络请求）")

# ---------------------------------------------------------------- D1
hr("规则层延迟：漏斗 ①~⑤ 全部同步完成，物理上不可能触达 LLM")
warm = ["我回家了", "好热", "打开客厅灯"]
for t in warm:
    agent.decide(t)                      # 预热

samples = []
for _ in range(20):
    for t in ["我回家了", "看电影", "好热", "打开客厅灯", "空调调到24度", "电视音量30"]:
        t0 = time.perf_counter()
        agent.decide(t)
        samples.append((time.perf_counter() - t0) * 1000)
samples.sort()
avg = sum(samples) / len(samples)
p95 = samples[int(len(samples) * 0.95)]
print(f"  样本 {len(samples)} 次　avg {avg:.1f}ms　p95 {p95:.1f}ms　max {samples[-1]:.1f}ms")
note("红线是 avg<10ms / p95<100ms。延迟主要由动作条数决定（每条含执行+回读+审计）")

# ---------------------------------------------------------------- D2
hr("五级漏斗：精确规则优先于宽泛规则（顺序本身就是被测对象）")
for text in ["窗帘开一半", "窗帘拉开", "太亮了", "卧室开灯"]:
    plan = agent.root.plan(text, {}, [])
    acts = "、".join(f"{s.device_id}.{s.command}{s.params or ''}" for s in plan.sub_tasks)
    print(f"  「{text}」→ {acts or '（无动作）'}")
note("「窗帘开一半」必须是 position=50（能力契约给的值），")
note("而不是被宽泛的设备正则 `窗帘.*(开|拉开)` 抢成全开 —— 规则越宽泛越靠后")

# ---------------------------------------------------------------- D3
hr("条件句保护：诚实拒绝，而不是误执行")
for text in ["如果客厅没人了就把灯关掉吧", "要是太热就开空调", "我把灯关掉吧"]:
    plan = agent.root.plan(text, {}, [])
    verdict = "执行" if plan.is_command else "拒绝（不产生任何设备动作）"
    print(f"  「{text}」→ {verdict}")
    if not plan.is_command:
        print(f"      回复：{plan.reply[:60]}")
note("条件指令要的是「创建自动化规则」，不是「立刻动作」。")
note("系统诚实说明并指路，而不是静默执行一条用户没要求的动作")

# ---------------------------------------------------------------- D4
hr("高危二次确认：未确认不执行")
res = agent.decide("把门锁打开")
print(f"  回复：{res['reply'][:70]}")
print(f"  requires_confirmation = {res['requires_confirmation']}")
print(f"  pending_id = {res['pending_id']}")
print(f"  门锁 locked 属性 = {registry.get('lock_entry_01').attributes['locked'].value}")
note("高危动作（门锁/燃气灶/摄像头直播/门锁模式，共 4 类）必须人工确认")
note("注意：事件路由是无人确认的路径，因此它对高危动作**默认拒绝**（缺陷 D23 的修复）")

# ---------------------------------------------------------------- D5
hr("分层延迟：规则层与慢环必须分开报")
print("  规则层（漏斗命中，确定性路径）：")
llm_log.reset()
for t in ["我回家了", "看电影", "好热", "打开客厅灯"]:
    agent.decide(t)
m = llm_log.metrics()
rp = m["rule_path"]
print(f"     样本 {rp['samples']}　avg {rp['avg_ms']}ms　p95 {rp['p95_ms']}ms")
print(f"  慢环：本次演示未走（见 docs/BENCH.md §四：实测 8–13s，瓶颈在模型生成速度）")
note("把两者混成一个「平均响应时间」，得到的数字既不解释性能也不解释体验")
note("取数来源：GET /api/metrics/llm —— 评委可当场调这个接口对")

# ---------------------------------------------------------------- D6
hr("多模态融合：模态冲突 → 整组丢弃（一致性复核）")
svc = PerceptionService()
conflict = PerceptionContext(
    scene_hint="无人",                                    # 视觉：低唤醒
    sensors={"temp_indoor": 23.0, "humidity": 50.0, "air_quality": 85.0},
    recent_actions=[{"a": 1}] * 8,                         # 环境行为：高唤醒
    habit={"routine_deviation": 1.0},
    presence_minutes=120.0, idle_minutes=5.0,
    now=__import__("datetime").datetime(2026, 10, 2, 14, 0),
)
r = svc.perceive(conflict)
print(f"  模态读数：{[(x.modality, x.label) for x in r.readings]}")
print(f"  融合距离：{r.gate.get('evidence', {}).get('modal_distance')}")
print(f"  门控通过：{r.gate['passed']}　原因：{r.gate.get('reason', '')[:60]}")
print(f"  建议：{r.suggestion}")
note("模态间最大两两欧氏距离 > 0.5 即判为冲突，整组结果丢弃")
note("零信息时返回零置信度并拒绝，而不是回退到 0.5（这是与上游的一处有意识偏离）")

normal = PerceptionContext(
    sensors={"temp_indoor": 23.0, "humidity": 50.0, "air_quality": 85.0},
    recent_actions=[{"a": 1}] * 3, habit={"routine_deviation": 0.2},
    presence_minutes=120.0, idle_minutes=10.0,
    now=__import__("datetime").datetime(2026, 10, 2, 14, 0),
)
r2 = svc.perceive(normal)
print(f"\n  无冲突时：门控通过 = {r2.gate['passed']}　情境 = {r2.situation['region_label']}")

# ---------------------------------------------------------------- D7
hr("白盒情境参数：每个参数都能展开原始依据（前端可点开看）")
for key, p in r2.situation["params"].items():
    print(f"  · {p['label']}: {p['value']}{p.get('unit', '')}")
    print(f"      真值来源：{p['truth']}")
    print(f"      计算依据：{p['source']}")
note("这是把「可解释性」从口号变成可查证据的地方，也是答辩最该演示的面板")

# ---------------------------------------------------------------- D8
hr("记忆召回：慢环规划时注入了哪条历史案例（可复核）")


class DemoLLM:
    """鸭子类型的假模型：驱动慢环但不联网。

    **替换的是 `agent.root.llm` 而不是 `agent.llm.chat_json`。**
    之前把 `agent.llm`（同一个 `LLMClient` 实例）的 `chat_json` 与 `available`
    直接改掉，看起来很省事，但它同时让 `LLMClient.offline=True` 这道闸门失效 ——
    于是后面 D9 调 `llm.chat_text()` 时**真的发出了网络请求**（实测 4.3 秒返回），
    而脚本末尾还印着"全部 10 项均未发起任何网络请求"。
    Root 持有自己的 llm 引用，只换它就能让 D8 走假模型、D9 仍用离线客户端。
    """

    def __init__(self):
        self.available = True
        self.offline = True
        self.last_used_backend = "demo(fake)"

    def chat_json(self, messages, **kw):
        return {"is_command": True, "reply": "已为您调暗灯光并开启低速风扇。",
                "actions": [{"device_id": "light_living_01",
                             "command": "set_brightness", "params": {"brightness": 20}},
                            {"device_id": "fan_living_01",
                             "command": "set_speed", "params": {"speed": "low"}}]}

    def chat_text(self, messages, **kw):
        return ("（这是假模型的输出示例）今天客厅的灯和空调用得最多，晚上观影模式开了两次。"
                "卧室通风略少，建议睡前开一会儿风扇。")


SLOW_INPUT = "把整个屋子的氛围弄温馨一点"
agent.root.llm = DemoLLM()          # 只替换 Root 的引用，agent.llm 保持真实且离线

first = agent.decide(SLOW_INPUT)
print(f"  第 1 次「{SLOW_INPUT}」→ {first['reply'][:40]}")
print(f"      执行动作数：{len(first['actions'])}"
      f"　经验库现存量：{experience_mod.experience.stats()['total']}")
rec = experience_mod.experience.retrieve(query=SLOW_INPUT, limit=1, min_rating=0)
if rec:
    print(f"      写回经验：id={rec[0]['id']}　评分={rec[0]['rating']}/5"
          f"　评分来源={rec[0]['rating_source']}")

second = agent.decide(SLOW_INPUT)
recall = next((t for t in second["decision_trace"] if t.get("step") == "recall"), None)
print(f"\n  第 2 次同一请求的决策链路：")
for t in second["decision_trace"]:
    print(f"      [{t['step'].upper():8s}] {t['detail'][:60]}")
note("RECALL 步写明本轮参考了哪条案例、评分多少 —— 经验复用是可复核的事实而不是声称")
note("评分由执行结果推导（全成功5/部分3/全失败1），衡量「执行成功了吗」而非「用户满意吗」")

# ---------------------------------------------------------------- D9
hr("外环日报：LLM 优先 / 模板降级，且降级可见")
store = agent.store
for t in ["我回家了", "看电影", "睡觉了"]:
    agent.decide(t)
daily_summary.LATEST_PATH = __import__("pathlib").Path(
    os.environ["LINGXI_DATA_DIR"]) / "demo_latest.json"
daily_summary.REPORT_PATH = __import__("pathlib").Path(
    os.environ["LINGXI_DATA_DIR"]) / "demo_daily.jsonl"

# ① 降级侧：用真实的离线客户端跑，走的就是"没有 Key / 断网"时的路径
report = daily_summary.run(registry, store, agent.llm, {}, window_hours=24)
print(f"  [降级路径] 生成方式：{report['source']}　耗时 {report['duration_ms']}ms")
print(f"      降级原因：{report['llm']['degrade_reason'][:70]}")
print(f"      日报正文：")
for line in report["narrative"].split("。"):
    if line.strip():
        print(f"          {line.strip()}。")
print(f"      事实依据：动作 {report['facts']['actions']['total']} 次"
      f"　采样点 {report['facts']['sensors'].get('samples', 0)} 个")
note("模板文案也必须有真实信息量 —— 否则「断网也能演示」就等于不存在")
print()

# ② 模型侧：用假模型演示 LLM 路径有内容，**不联网**
facts = report["facts"]
fake = daily_summary.generate(DemoLLM(), facts)
print(f"  [模型路径] 生成方式：{fake['source']}　（假模型，本脚本不联网）")
print(f"      日报正文：{fake['narrative']}")
note("返回体如实标注 source=llm/template，系统不假装自己用了大模型")
note("降级原因区分三种情况：未配置 Key / 离线模式 / 调用了但失败")

# ---------------------------------------------------------------- D10
hr("内环告警 → 环境联动（确定性阈值，与感知层性质不同）")
alerts = AlertEvaluator()
hotsensors = {"temp_indoor": 33.0, "humidity": 55, "air_quality": 85, "light_level": 400}
events = alerts.evaluate(hotsensors, registry.all())
print(f"  读数：temp_indoor = 33.0℃")
for e in events:
    print(f"  事件：{e['type']} / {e['payload']['kind']}")
    print(f"      文案：{e['payload']['summary']}")

router = EventRouter(registry, DemoManager(), state_store=StateStore())
import asyncio                                                          # noqa: E402
ev = asyncio.run(asyncio.to_thread(router.route, events[0]))
ac = registry.get("ac_living_01")
print(f"\n  联动结果：空调 on={ac.attributes['on'].value}"
      f"　温度={ac.attributes['temperature'].value}℃")
for a in ev.actions:
    print(f"      [{a.get('type')}] {a.get('device_id', '')} "
          f"{a.get('command', '')} → {a.get('status', a.get('message', ''))[:40]}")

note("阈值在 config/alert_routes.json（可复核），不是硬编码在 Python 里")
print()
print("  ⚠️  必须能当场分辨的区别：")
print("      内环告警 = 确定性阈值判定（读数比大小）→ 允许触发设备动作")
print("      感知层   = 多模态统计推断（失效模式不透明）→ 绝不触发设备动作")
print("      二者外观相似、性质不同。见 docs/架构图.md 图 8")

# ---------------------------------------------------------------- 收尾
# 「未联网」不能只是一句口号：用观测层的调用计数**校验**它。
# 这不是装饰 —— 本脚本初版就是因为直接把 agent.llm 换成假模型，绕过了
# LLMClient.offline 这道闸门，D9 真的发出了 4.3 秒的网络请求，
# 而末尾还印着"未发起任何网络请求"。加这条断言后，同类问题会当场暴露。
_obs_calls = llm_log.metrics()["llm_calls"]["calls"]
print(f"\n{'=' * 70}")
if _obs_calls == 0:
    print("  演示结束。观测层记录的模型调用次数 = 0 ✓（确实没有发起任何网络请求）")
else:
    print(f"  ⚠️  演示结束，但观测层记录到 {_obs_calls} 次模型调用 ——")
    print("      本脚本声称不联网，这里有矛盾，请检查是否有演示点绕过了离线闸门。")
print(f"{'=' * 70}")
print("\n  配套文档：")
print("    docs/融合技术白皮书.md      融合叙事与创新点对照表")
print("    docs/架构图.md              8 张架构图（含统一门控抽象）")
print("    docs/BENCH.md               性能数据与采集方法")
print("    docs/感知层可靠性报告.md    逐模态验证边界（含上游负面数据）")
print("    docs/为什么不用LangChain.md 选型论证")
print("    docs/答辩Q&A预案.md         20 个问答 + 危险表述清单")
