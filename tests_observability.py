# -*- coding: utf-8 -*-
"""可观测性三件套测试：限流 / 接口统计 / 模型调用与分层延迟。

为什么这组测试值得单独存在：

1. **限流阈值是本项目自己踩过的坑**。心镜写死 60 次/分钟，而灵犀的前端是 3D
   孪生沙盒，一次页面刷新就要拉 three.module.js + 10 个 GLB 模型 + 多个 JS/CSS，
   再叠加 10s 一次的感知轮询 —— 照搬 60/min 的结果是"演示时页面刷不出来"。
2. **加锁不是形式**。心镜的窗口记录是模块级 `defaultdict`，在
   `asyncio.to_thread` 的线程池下并发读写同一 list。这里用真实多线程压一遍，
   断言放行总数**恰好**等于限额 —— 竞态的表现就是"偶尔多放行几个"。
3. **分层延迟是本项目全部性能说法的取数来源**。规则层与慢环必须分开统计，
   混在一起算出来的是"平均响应时间"这种既不能解释性能也不能解释体验的数字。
"""
import os
import sys
import tempfile
import threading

_TMP_DIR = tempfile.mkdtemp(prefix="lx_obs_")
os.environ["LINGXI_DATA_DIR"] = _TMP_DIR
os.environ["LLM_OFFLINE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
from pathlib import Path

from backend.observability.api_log import ApiLog
from backend.observability.llm_log import LLMLog, percentile
from backend.observability.rate_limit import RateLimiter

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
print("  灵犀 — 可观测性三件套测试")
print("=" * 60)

# ---------------------------------------------------------------- 1. 限流
print("\n== 1. 限流（IP 滑窗） ==")
lim = RateLimiter(max_per_minute=5, llm_max_per_minute=2)
allowed = [lim.check("1.1.1.1", "/api/devices")[0] for _ in range(7)]
check("普通端点：前 5 次放行、第 6 次起拒绝",
      allowed == [True] * 5 + [False, False], str(allowed))
check("拒绝计数已统计", lim.rejected == 2)
check("其他 IP 互不影响", lim.check("2.2.2.2", "/api/devices")[0] is True)
check("retry_after >= 1 秒", lim.retry_after("1.1.1.1", "/api/devices") >= 1)

lim2 = RateLimiter(max_per_minute=100, llm_max_per_minute=2)
check("LLM 端点用独立（更严）的额度",
      [lim2.check("3.3.3.3", "/api/chat")[0] for _ in range(3)] == [True, True, False])
check("LLM 额度用尽不影响普通端点",
      lim2.check("3.3.3.3", "/api/devices")[0] is True)
check("LLM 端点前缀识别", lim2.is_llm_endpoint("/api/chat") and
      lim2.is_llm_endpoint("/api/jobs/daily_summary/run") and
      not lim2.is_llm_endpoint("/api/devices"))
check("健康检查豁免", lim2.is_exempt("/mcp/health"))

# 窗口滑出后可再次放行（用显式时间戳，不 sleep）
lim3 = RateLimiter(max_per_minute=1)
t0 = 1_000_000.0
check("窗口内第一次放行", lim3.check("4.4.4.4", "/x", now=t0)[0] is True)
check("窗口内第二次拒绝", lim3.check("4.4.4.4", "/x", now=t0 + 10)[0] is False)
check("61 秒后窗口滑出、重新放行", lim3.check("4.4.4.4", "/x", now=t0 + 61)[0] is True)

# 并发：多线程同时打同一个 IP，放行总数必须**恰好**等于限额
lim4 = RateLimiter(max_per_minute=50)
results = []
lock = threading.Lock()


def hammer():
    local = [lim4.check("9.9.9.9", "/api/devices")[0] for _ in range(20)]
    with lock:
        results.extend(local)


threads = [threading.Thread(target=hammer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("★ 并发 160 次请求，放行数恰好等于限额 50（无竞态）",
      sum(1 for r in results if r) == 50,
      f"实际放行 {sum(1 for r in results if r)} / 共 {len(results)} 次")
check("窗口记录未超限（list 未被并发写坏）",
      len(lim4._windows["9.9.9.9"]) == 50, str(len(lim4._windows["9.9.9.9"])))

# ---------------------------------------------------------------- 2. API 统计
print("\n== 2. API 统计 ==")
log = ApiLog(path=Path(_TMP_DIR) / "acc.jsonl")
log.record("GET", "/api/devices", 200, 3.2, "127.0.0.1")
log.record("GET", "/api/devices", 200, 4.1, "127.0.0.1")
log.record("POST", "/api/chat", 500, 120.5, "127.0.0.1", error="ValueError: boom")
st = log.stats()
check("总请求数", st["total_requests"] == 3, str(st["total_requests"]))
check("错误数", st["total_errors"] == 1)
check("错误率", abs(st["error_rate"] - 1 / 3) < 0.001, str(st["error_rate"]))
check("端点分布排序", st["top_endpoints"][0]["endpoint"] == "/api/devices")
check("状态码分布", st["status_distribution"] == {"200": 2, "500": 1},
      str(st["status_distribution"]))
check("平均延迟", abs(st["avg_latency_ms"] - 42.6) < 0.2, str(st["avg_latency_ms"]))
check("最近错误已记录", st["recent_errors"] and "boom" in st["recent_errors"][0]["error"])

lines = (Path(_TMP_DIR) / "acc.jsonl").read_text(encoding="utf-8").strip().splitlines()
check("落盘为 JSONL（每行一条，可直接过滤）",
      len(lines) == 3 and json.loads(lines[0])["path"] == "/api/devices", str(len(lines)))

# 端点数量上限：扫描器打过来的随机路径不该把内存吃光
log2 = ApiLog(path=Path(_TMP_DIR) / "acc2.jsonl", enabled=False)
for i in range(500):
    log2.record("GET", f"/random/path/{i}", 404, 1.0)
check("端点键数量有上限（防内存无限增长）",
      len(log2._stats["endpoints"]) <= 200, str(len(log2._stats["endpoints"])))
check("延迟样本环形缓冲有上限",
      len(log2._stats["recent_latency"]) <= 200, str(len(log2._stats["recent_latency"])))
log.reset()
check("reset 后清零", log.stats()["total_requests"] == 0)

# 落盘失败不得影响被观测的请求
bad = ApiLog(path=Path(_TMP_DIR) / "no_such_dir" / "\0bad" / "x.jsonl")
bad.record("GET", "/x", 200, 1.0)
check("落盘失败不抛异常（观测层不拖垮被观测的请求）",
      bad.stats()["total_requests"] == 1)

# ---------------------------------------------------------------- 3. 模型调用 / 分层延迟
print("\n== 3. 模型调用与分层延迟 ==")
check("分位数：空样本返回 0", percentile([], 95) == 0)
check("分位数：单样本返回该值", percentile([7.0], 95) == 7.0)
check("分位数：最近秩法（100 样本 p95 = 96）",
      percentile([float(i) for i in range(1, 101)], 95) == 96.0,
      str(percentile([float(i) for i in range(1, 101)], 95)))
check("分位数 p100 = 最大值", percentile([1.0, 5.0, 3.0], 100) == 5.0)
check("分位数结果必然来自真实样本",
      percentile([1.0, 2.0, 3.0, 4.0], 95) in (1.0, 2.0, 3.0, 4.0))

llm = LLMLog(path=Path(_TMP_DIR) / "llm.jsonl")
for i in range(1, 21):
    # parse_ms 是漏斗纯解析（亚毫秒量级），total_ms 是 decide 端到端（含执行与审计）
    llm.record_decision(source="rule" if i % 2 else "llm",
                        parse_ms=float(i) / 100, total_ms=float(i) * 100,
                        degraded=(i % 4 == 0))
llm.record_call(1200.0, True, "zhipu", "glm-4-flash", prompt_chars=1500,
                output_chars=300, kind="chat_json")
llm.record_call(2000.0, False, "zhipu", "glm-4-flash",
                degrade_reason="Timeout", kind="chat_json")

m = llm.metrics()
check("规则层样本只含非慢环决策", m["rule_path"]["samples"] == 10,
      str(m["rule_path"]["samples"]))
check("慢环样本只含慢环决策", m["slow_path"]["samples"] == 10,
      str(m["slow_path"]["samples"]))
check("★ 规则层与慢环延迟量级分离（这是分层报告的全部意义）",
      m["rule_path"]["avg_ms"] < m["slow_path"]["avg_ms"],
      f"{m['rule_path']['avg_ms']} vs {m['slow_path']['avg_ms']}")
check("规则层 avg 为**端到端**口径（含执行与审计）",
      m["rule_path"]["avg_ms"] == 1000.0, str(m["rule_path"]["avg_ms"]))
check("★ 规则层同时给出漏斗纯解析口径（两者差两个数量级，同名不同义最难查）",
      m["rule_path"]["parse"]["avg_ms"] == 0.1
      and m["rule_path"]["parse"]["avg_ms"] < m["rule_path"]["avg_ms"] / 100,
      f"parse={m['rule_path']['parse']['avg_ms']} vs total={m['rule_path']['avg_ms']}")
check("慢环 avg 正确", m["slow_path"]["avg_ms"] == 1100.0, str(m["slow_path"]["avg_ms"]))
check("p95 已计算",
      m["rule_path"]["p95_ms"] > 0 and m["slow_path"]["p95_ms"] > 0
      and m["rule_path"]["parse"]["p95_ms"] > 0)
check("note 明确说明 avg/p95 是端到端口径（避免读者再混淆）",
      "端到端" in m["rule_path"]["note"] and "parse" in m["rule_path"]["note"],
      m["rule_path"]["note"])
check("调用次数与成功/失败分开计",
      m["llm_calls"]["calls"] == 2 and m["llm_calls"]["ok"] == 1
      and m["llm_calls"]["failed"] == 1, str(m["llm_calls"]["calls"]))
check("成功率", m["llm_calls"]["success_rate"] == 0.5)
check("★ 降级率可算（这是「降级必须可见」的落点）",
      m["llm_calls"]["degrade_rate"] == 0.25, str(m["llm_calls"]["degrade_rate"]))
check("token 估算非零且注明是估算",
      m["llm_calls"]["output_tokens"] > 0 and "估算" in m["llm_calls"]["note"])
check("决策来源分布", m["source_distribution"] == {"rule": 10, "llm": 10},
      str(m["source_distribution"]))
# 分层口径：只有 source == "llm" 才算慢环，其余（rule / fallback）都是确定性路径。
# fallback 指的是"空输入/条件句/规则未命中且未上云"——它们同样在 plan() 内同步返回、
# 同样没碰模型，所以归入规则层统计是正确口径，不是污染。
llm.record_decision("fallback", 0.1, 2.0)
_after = llm.metrics()
check("非 LLM 来源（含 fallback）都计入规则层，慢环样本不受影响",
      _after["rule_path"]["samples"] == 11 and _after["slow_path"]["samples"] == 10,
      f"rule={_after['rule_path']['samples']} slow={_after['slow_path']['samples']}")

call_lines = (Path(_TMP_DIR) / "llm.jsonl").read_text(encoding="utf-8").strip().splitlines()
check("模型调用逐次落盘 JSONL", len(call_lines) == 2, str(len(call_lines)))
check("日志含降级原因（事后可复盘）",
      "Timeout" in call_lines[1] and "degrade_reason" in call_lines[1])
check("recent_calls 可查最近调用", len(llm.recent_calls(limit=5)) == 2)
llm.reset()
check("reset 后指标清零",
      llm.metrics()["llm_calls"]["calls"] == 0
      and llm.metrics()["rule_path"]["samples"] == 0)

# 未配置 Key 时的降级也要被记录（否则"LLM 层无声消失"依然查不出来）
llm2 = LLMLog(path=Path(_TMP_DIR) / "llm2.jsonl")
llm2.record_call(0.0, False, "zhipu", "glm-4-flash", degrade_reason="后端不可用")
check("失败调用同样计入（成功率为 0 而不是「没有数据」）",
      llm2.metrics()["llm_calls"]["success_rate"] == 0.0
      and llm2.metrics()["llm_calls"]["calls"] == 1)
check("失败原因保留在日志里（可区分未配置 Key 与网络超时）",
      llm2.recent_calls(1)[0]["degrade_reason"] == "后端不可用")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
