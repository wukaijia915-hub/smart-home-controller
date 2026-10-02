# -*- coding: utf-8 -*-
"""基准数据采集器 —— `docs/BENCH.md` 里每一个数字的来源。

**为什么要有这个脚本，而不是在文档里手写数字**：
文档里的性能数字如果无法复现，它就只是一句声称。评委完全可以当场问"这个 7.4ms
怎么测的、在什么机器上、样本多少"，答不上来整份 BENCH 的可信度就没了。
这个脚本把采集方法与原始数据一起固化下来：阅读者跑一遍就能得到同一量级的数字，
且每次输出都带机器信息与样本量。

用法：
    LLM_OFFLINE=1 python experiments/bench_collect.py
    # 写出 experiments/bench_results.json（原始样本 + 汇总）

**本脚本不发起任何网络请求**（不测慢环 LLM 端到端延迟）。理由有两层：
① 慢环延迟由服务商生成速度主导，会随模型、时段、配额波动，一次测量不代表长期表现；
② 那部分数据来自 `experiments/README.md` 实验五的对照实验（记录了 prompt/输出
   token 与耗时的对应关系），BENCH.md 会标注其采集日期与方法，而不是把它伪装成
   本次测量结果。需要现场重测时单独跑 `python tests_performance.py --api`。
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("LLM_OFFLINE", "1")     # 双保险：本脚本绝不联网

from backend.agent import HomeAgent                                    # noqa: E402
from backend.alerts import AlertEvaluator                              # noqa: E402
from backend.capabilities import CapabilityRegistry                    # noqa: E402
from backend.devices.registry import DeviceRegistry                    # noqa: E402
from backend.jobs import daily_summary                                 # noqa: E402
from backend.memory import experience as experience_mod                # noqa: E402
from backend.memory import habits as habits_mod                        # noqa: E402
from backend.memory import manuals as manuals_mod                      # noqa: E402
from backend.perception import PerceptionContext, PerceptionService    # noqa: E402
from backend.toolkit import registry as tool_registry_mod              # noqa: E402

OUT_PATH = Path(__file__).resolve().parent / "bench_results.json"


def pct(values: list[float], p: float) -> float:
    """最近秩法分位数（与 observability/llm_log.py 同一口径）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(p / 100.0 * len(ordered) + 0.5)) - 1))
    return round(ordered[k], 3)


def summarize(values: list[float]) -> dict:
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        "avg": round(statistics.fmean(values), 3),
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "max": round(max(values), 3),
    }


def timeit(fn, repeat: int = 1) -> float:
    t0 = time.perf_counter()
    for _ in range(repeat):
        fn()
    return (time.perf_counter() - t0) * 1000.0 / repeat


# ---------------------------------------------------------------- 采集项
RULE_INPUTS = [
    "我回家了", "到家了", "我出门了", "我走了", "看电影", "看个片", "睡觉了", "晚安",
    "有客人来", "早上好", "好热", "太冷了", "省电模式", "打开客厅灯", "卧室开灯",
    "关闭空调", "窗帘拉开", "窗帘开一半", "空调调到24度", "电视音量30", "打开风扇",
    "加湿器打开", "氛围弄温馨一点", "你好呀", "今天天气怎么样",
]


def bench_rule_layer(runs: int = 60) -> dict:
    """规则层延迟：`HomeAgent.decide()` 的端到端（含执行 + 审计）。

    这一项覆盖的正是「规则层 <100ms」这条红线。注意测的是 `decide` 而不是
    `root.plan`：前者才是用户实际感受到的响应时间。

    **必须按漏斗来源分组**：`RULE_INPUTS` 里故意放了两条漏斗打不住的输入
    （"氛围弄温馨一点"、"今天天气怎么样"），它们会走到慢环。把这两条和规则命中的
    混在一起取平均，得到的既不是"规则层延迟"也不是"端到端延迟"，而是一个
    没有对应实体的数字。所以先记下每次实际命中的 source，再分组统计。
    """
    registry = DeviceRegistry()
    caps = CapabilityRegistry()
    caps.auto_pair(registry.all())
    agent = HomeAgent(registry, capabilities=caps)

    # 预热：首次调用会触发模块内懒加载，混进来会把 p95 拉高
    for text in RULE_INPUTS:
        agent.decide(text)

    samples: dict[str, list[float]] = {}
    by_source: dict[str, list[float]] = {}
    for _ in range(runs):
        for text in RULE_INPUTS:
            ms = timeit(lambda t=text: agent.decide(t))
            samples.setdefault(text, []).append(ms)
            src = agent.root.plan(text, {}, []).source
            by_source.setdefault(src, []).append(ms)
    return {
        "method": f"HomeAgent.decide() 端到端，{len(RULE_INPUTS)} 条输入 × {runs} 轮，"
                  f"各轮前先整体预热一次；按漏斗实际命中的 source 分组统计",
        "by_source": {k: summarize(v) for k, v in sorted(by_source.items())},
        "rule_only": summarize(by_source.get("rule", [])),
        "per_input": {k: summarize(v) for k, v in sorted(samples.items())},
    }


def bench_plan_only(runs: int = 200) -> dict:
    """漏斗 ①~⑤ 纯解析延迟（不含设备执行与审计）。"""
    registry = DeviceRegistry()
    caps = CapabilityRegistry()
    caps.auto_pair(registry.all())
    agent = HomeAgent(registry, capabilities=caps)
    for text in RULE_INPUTS:
        agent.root.plan(text, {}, [])
    ms = [timeit(lambda t=t: agent.root.plan(t, {}, [])) for _ in range(runs)
          for t in RULE_INPUTS]
    return {"method": f"RootAgent.plan()，{len(RULE_INPUTS)} 条 × {runs} 轮",
            **summarize(ms)}


def bench_control_success() -> dict:
    """设备控制成功率 + 高危挂起率。

    **口径必须说清楚**：高危动作（门锁 unlock、燃气灶点火）走的是"挂起待人工确认"，
    它返回 `ok=False` 但**这不是失败** —— 那是设计要的行为。把两者混成一个
    "成功率"会得出 90% 这种既冤枉系统又掩盖真相的数字。所以这里分三档统计：
    已执行成功 / 高危挂起（预期行为）/ 真失败（应当为 0）。
    """
    registry = DeviceRegistry()
    caps = CapabilityRegistry()
    caps.auto_pair(registry.all())
    agent = HomeAgent(registry, capabilities=caps)
    from backend.toolkit.guard import ManagerGuard
    from backend.tools import tool_registry
    tool_registry.build_from_devices(registry)
    tool_registry.bind_actuation_guard(ManagerGuard(agent.manager))

    ok = suspended = 0
    failures, suspended_tools = [], []
    total = 0
    for t in tool_registry.list_tools():
        if t.category != "device":
            continue                      # 非设备类工具（场景/元工具）另行处理
        total += 1
        params = {}
        for p in t.parameters:
            if not p.required:
                continue
            if p.enum:
                params[p.name] = p.enum[0]
            elif p.type == "number":
                params[p.name] = p.min if isinstance(p.min, (int, float)) else 1
            elif p.type == "boolean":
                params[p.name] = True
            else:
                params[p.name] = "auto"
        r = tool_registry.call(t.name, params)
        if r.get("ok"):
            ok += 1
        elif r.get("requires_confirmation"):
            suspended += 1
            suspended_tools.append(t.name)
        else:
            failures.append({"tool": t.name, "error": str(r.get("error", ""))[:120]})
    executed = ok + suspended
    return {
        "method": "对每个设备类工具按其契约构造一次最小合法参数调用；"
                  "高危动作走「挂起待确认」故单独计",
        "device_tools": total,
        "ok": ok,
        "suspended_high_risk": suspended,
        "suspended_tools": suspended_tools,
        "failed": len(failures),
        # 两个口径都给，避免"成功率 93.75%"这种既冤枉系统又掩盖真相的读法：
        # 高危挂起是设计要的行为，不是失败。对外声称"控制成功率 100%"时
        # 必须说明它的口径是"非被安全机制拦截的动作"。
        "success_rate_non_high_risk": round(ok / max(executed, 1), 4),
        "success_rate_all_tools": round((ok + suspended) / max(total, 1), 4),
        "failure_examples": failures[:5],
    }


def bench_perception() -> dict:
    """感知层：多模态融合 + 门控 + 5 个情境参数。"""
    svc = PerceptionService()
    ctx = PerceptionContext(
        sensors={"temp_indoor": 23.0, "humidity": 50.0, "air_quality": 85.0},
        recent_actions=[{"device_id": "light_living_01", "command": "turn_on"}] * 5,
        habit={"routine_deviation": 0.2},
        presence_minutes=120.0, idle_minutes=5.0,
    )
    svc.perceive(ctx)     # 预热
    ms = [timeit(lambda: svc.perceive(ctx)) for _ in range(300)]
    return {"method": "PerceptionService.perceive()（4 模态 → 融合 → 门控 → 5 参数）",
            **summarize(ms)}


def bench_memory() -> dict:
    """记忆层三源检索延迟。"""
    manuals = manuals_mod.ManualStore()
    out = {}
    ms = [timeit(lambda: manuals.search("空调怎么省电")) for _ in range(300)]
    out["手册检索"] = summarize(ms)

    exp = experience_mod.ExperienceStore()
    for i in range(200):
        exp.record("fuzzy_command", f"客厅|ac{i % 7}", f"有点闷第{i}次",
                   [{"device_id": "ac_living_01", "command": "turn_on"}],
                   "已开启", rating=5 - (i % 3))
    ms = [timeit(lambda: exp.retrieve(query="有点闷", limit=2)) for _ in range(200)]
    out["经验检索"] = summarize(ms)
    ms = [timeit(lambda: exp.build_context(prefer_category="fuzzy_command",
                                           query="有点闷")) for _ in range(200)]
    out["经验注入构造"] = summarize(ms)
    ms = [timeit(lambda: habits_mod.HabitStore().stats()) for _ in range(50)]
    out["习惯统计"] = summarize(ms)
    return {"method": "各源单次调用（经验库预置 200 条案例）", **out}


def bench_alerts_and_jobs() -> dict:
    """内环告警判定 + 外环日报生成（模板降级路径）。"""
    ev = AlertEvaluator()
    sensors = {"temp_indoor": 32.0, "air_quality": 85, "humidity": 55, "light_level": 400}
    registry = DeviceRegistry()
    ev.evaluate(sensors, registry.all())
    ms = [timeit(lambda: ev.evaluate(sensors, registry.all())) for _ in range(300)]

    store = __import__("backend.state", fromlist=["StateStore"]).StateStore()
    for i in range(300):
        store.bump()
        store.record(None, "light_living_01", "turn_on", {},
                     {"ok": True, "device_id": "light_living_01"})
    ms2 = [timeit(lambda: daily_summary.collect_facts(registry, store, {}))
           for _ in range(50)]
    ms3 = [timeit(lambda: daily_summary.render_template(
        daily_summary.collect_facts(registry, store, {}))) for _ in range(50)]
    return {
        "method": "告警判定含冷却检查；日报事实采集基于 300 条审计记录",
        "内环告警判定": summarize(ms),
        "日报事实采集": summarize(ms2),
        "日报模板渲染": summarize(ms3),
    }


def bench_funnel_static() -> dict:
    """静态不变量的执行规模（供文档说明测试覆盖到什么程度）。"""
    root = Path(__file__).resolve().parent.parent
    counts = {}
    for d in ("backend/perception", "backend/memory", "backend/observability",
              "backend/toolkit", "backend/agents", "backend/jobs"):
        p = root / d
        counts[d] = {
            "py_files": len(list(p.glob("*.py"))),
            "lines": sum(len(f.read_text(encoding="utf-8").splitlines())
                         for f in p.glob("*.py")),
        }
    return counts


def bench_slow_loop_recorded() -> dict:
    """慢环延迟：**引用已有的对照实验数据，不重新联网测量**。

    见 `experiments/README.md` 实验五。这里只把结论结构化，方便 BENCH.md 引用；
    重测命令写在 note 里。
    """
    return {
        "source": "experiments/README.md 实验五（2026-10-02 采集）",
        "model": "glm-4-flash",
        "note": "未在本脚本内重测：慢环延迟由服务商生成速度主导，随模型/时段/配额波动，"
                "单次测量不代表长期表现。重测命令：python tests_performance.py --api",
        "finding": "耗时 ≈ 1.2s 基线 + 输出 token ÷ 15；与调用次数、prompt 体积无关",
        "prompt_volume_experiment": [
            {"prompt_tokens": 51, "completion_tokens": 17, "ms": 3551},
            {"prompt_tokens": 567, "completion_tokens": 16, "ms": 2834},
            {"prompt_tokens": 1113, "completion_tokens": 16, "ms": 1311},
            {"prompt_tokens": 1659, "completion_tokens": 17, "ms": 1800},
        ],
        "output_volume_experiment": [
            {"prompt_tokens": 51, "completion_tokens": 13, "ms": 1277},
            {"prompt_tokens": 51, "completion_tokens": 48, "ms": 2934},
            {"prompt_tokens": 51, "completion_tokens": 87, "ms": 6425},
            {"prompt_tokens": 51, "completion_tokens": 196, "ms": 11978},
        ],
    }


def main() -> int:
    print("=" * 68)
    print("  灵犀 — 基准数据采集（不联网）")
    print("=" * 68)
    started = datetime.now()
    results = {
        "collected_at": started.isoformat(timespec="seconds"),
        "env": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "llm_offline": os.environ.get("LLM_OFFLINE"),
        },
        "note": "所有延迟为单进程单线程顺序调用的墙上时间（ms）。"
                "样本量为顺序调用次数，未做并发压测；并发表现见 tests_observability.py。",
    }

    steps = [
        ("规则层端到端延迟", lambda: bench_rule_layer()),
        ("漏斗解析延迟", lambda: bench_plan_only()),
        ("设备控制成功率", bench_control_success),
        ("感知层融合+情境", bench_perception),
        ("记忆三源检索", bench_memory),
        ("内环告警与外环日报", bench_alerts_and_jobs),
        ("源码规模", bench_funnel_static),
        ("慢环延迟（引用记录）", bench_slow_loop_recorded),
    ]
    for label, fn in steps:
        t0 = time.perf_counter()
        try:
            results[label] = fn()
        except Exception as e:
            results[label] = {"error": f"{type(e).__name__}: {e}"}
            print(f"  ✗ {label}: {type(e).__name__}: {e}")
            continue
        print(f"  ✓ {label}  ({(time.perf_counter() - t0):.1f}s)")

    results["duration_seconds"] = round((datetime.now() - started).total_seconds(), 1)
    OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据已写出：{OUT_PATH}")

    # 摘要打印（供人工核对）
    r = results.get("规则层端到端延迟", {}).get("rule_only", {})
    print(f"\n规则层端到端（仅漏斗 source=rule）：avg {r.get('avg')}ms / "
          f"p50 {r.get('p50')}ms / p95 {r.get('p95')}ms / max {r.get('max')}ms"
          f"（{r.get('samples')} 样本）")
    for src, s in (results.get("规则层端到端延迟", {}).get("by_source") or {}).items():
        print(f"   source={src:9s} n={s.get('samples'):5d}  avg {s.get('avg')}ms  "
              f"p95 {s.get('p95')}ms")
    p = results.get("漏斗解析延迟", {})
    print(f"漏斗解析：avg {p.get('avg')}ms / p95 {p.get('p95')}ms")
    c = results.get("设备控制成功率", {})
    print(f"设备类工具：成功 {c.get('ok')} / 高危挂起 {c.get('suspended_high_risk')} / "
          f"真失败 {c.get('failed')}（非高危口径 "
          f"{c.get('success_rate_non_high_risk')}，全量口径 "
          f"{c.get('success_rate_all_tools')}）")
    if c.get("suspended_tools"):
        print(f"   挂起（预期行为，非失败）：{', '.join(c['suspended_tools'])}")
    if c.get("failure_examples"):
        print(f"   失败样例：{c['failure_examples']}")
    print(f"总耗时：{results['duration_seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
