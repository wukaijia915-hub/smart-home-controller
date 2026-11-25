# -*- coding: utf-8 -*-
"""慢环 LLM 预算测试。

预算存在的理由不是降延迟（已实测证伪：延迟由输出 token 数决定，与调用次数无关），
而是**给慢环一个有界上限**，并让降级可见：

- `.env` 的 `LLM_TIMEOUT=30` 让一次卡住的请求能占用工作线程 30 秒
- 静默降级（超时/熔断返回 None）与正常工作在返回体上长得一样

全部用例离线运行，用假 LLM 保证确定性。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.agent import HomeAgent
from backend.agents.llm_budget import LLMBudget
from backend.capabilities import CapabilityRegistry
from backend.devices.registry import DeviceRegistry

PASSED, FAILED = 0, 0


def check(label: str, condition: bool, detail: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {label}")
    else:
        FAILED += 1
        print(f"  ✗ {label}  {detail}")


class FakeLLM:
    """记录调用参数与次数的假 LLM。"""

    def __init__(self, result=None, delay: float = 0.0):
        self.result = result
        self.delay = delay
        self.calls: list[dict] = []

    def chat_json(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        return self.result


print("=" * 60)
print("  灵犀 — 慢环 LLM 预算测试")
print("=" * 60)

# ---------------------------------------------------------------- 1. 次数上限
print("\n== 1. 调用次数上限 ==")

b = LLMBudget(max_calls=2, max_wall_seconds=60, per_call_timeout=5)
b.begin()
llm = FakeLLM(result={"ok": True})

check("首次调用允许", b.allow_call()[0])
b.call(llm, [{"role": "user", "content": "x"}])
check("已记 1 次调用", b.state.calls == 1)

ok, why = b.allow_call()
check("第 2 次仍允许", ok)
b.call(llm, [{"role": "user", "content": "x"}])

ok, why = b.allow_call()
check("达到上限后不再允许", not ok and "次数上限" in why, why)

r = b.call(llm, [{"role": "user", "content": "x"}])
check("超限调用返回 None 且不触碰后端", r is None and len(llm.calls) == 2,
      f"后端被调用 {len(llm.calls)} 次")
check("超限被标记为降级", b.state.degraded and "次数上限" in b.state.reason, b.state.reason)

# ---------------------------------------------------------------- 2. 单次超时覆盖
print("\n== 2. 单次超时覆盖 .env 的 LLM_TIMEOUT ==")

b2 = LLMBudget(max_calls=3, per_call_timeout=7.5)
b2.begin()
llm2 = FakeLLM(result={"ok": True})
b2.call(llm2, [{"role": "user", "content": "x"}])
check("timeout 被透传给后端且等于预算值", llm2.calls[0].get("timeout") == 7.5,
      f"实际 {llm2.calls[0].get('timeout')}")

# 默认值应显著小于 .env 的 30s（否则超时保护形同虚设）
b3 = LLMBudget()
check("默认单次超时远小于 .env 的 30s", b3.per_call_timeout < 30,
      f"默认 {b3.per_call_timeout}s")
check("默认墙钟预算有界", 0 < b3.max_wall_seconds <= 60, f"{b3.max_wall_seconds}s")

# ---------------------------------------------------------------- 3. 墙钟预算
print("\n== 3. 墙钟预算 ==")

b4 = LLMBudget(max_calls=10, max_wall_seconds=0.2, per_call_timeout=5)
b4.begin()
llm4 = FakeLLM(result={"ok": True}, delay=0.25)
b4.call(llm4, [{"role": "user", "content": "x"}])
ok, why = b4.allow_call()
check("墙钟耗尽后不再允许调用", not ok and "墙钟" in why, why)

# ---------------------------------------------------------------- 4. 失败降级可见
print("\n== 4. 失败/降级必须可见 ==")

b5 = LLMBudget()
b5.begin()
llm5 = FakeLLM(result=None)      # 模拟超时/熔断/响应格式不符
b5.call(llm5, [{"role": "user", "content": "x"}])
check("后端返回 None → 标记降级", b5.state.degraded, str(b5.state))
check("降级原因非空", bool(b5.state.reason), b5.state.reason)

b6 = LLMBudget()
b6.begin()
check("正常调用不标记降级", not b6.state.degraded)

# ---------------------------------------------------------------- 5. begin 重置
print("\n== 5. begin() 重置预算周期 ==")

b7 = LLMBudget(max_calls=1)
b7.begin()
b7.call(FakeLLM(result={"ok": True}), [{"role": "user", "content": "x"}])
check("重置前已达上限", not b7.allow_call()[0])
b7.begin()
check("begin() 后调用数归零", b7.state.calls == 0)
check("begin() 后允许再次调用", b7.allow_call()[0])
check("begin() 清除降级标记", not b7.state.degraded)

# ---------------------------------------------------------------- 6. 接入 decide()
print("\n== 6. 接入 decide()：返回体带预算信息、降级写入 trace ==")

registry = DeviceRegistry()
caps = CapabilityRegistry()
caps.auto_pair(registry.all())
agent = HomeAgent(registry, capabilities=caps)

# 规则命中的输入不应产生任何 LLM 调用
res = agent.decide("打开客厅灯")
check("规则命中：返回体含 llm_budget", "llm_budget" in res, str(list(res.keys())))
check("规则命中：0 次 LLM 调用", res["llm_budget"]["calls"] == 0,
      str(res["llm_budget"]))
check("规则命中：未降级", res["llm_budget"]["degraded"] is False)

# 慢环：用假 LLM 让调用必然失败，验证降级在 trace 里可见
_orig_chat = agent.llm.chat_json
_orig_avail = agent.llm.available
agent.llm.chat_json = lambda messages, **kw: None
agent.llm.available = True
try:
    res2 = agent.decide("把整个屋子的氛围弄温馨一点")
finally:
    agent.llm.chat_json = _orig_chat
    agent.llm.available = _orig_avail

check("慢环失败：触发 1 次调用", res2["llm_budget"]["calls"] == 1, str(res2["llm_budget"]))
check("慢环失败：标记降级", res2["llm_budget"]["degraded"] is True, str(res2["llm_budget"]))
check("慢环失败：降级写入 decision_trace",
      any(t.get("step") == "degrade" for t in res2["decision_trace"]),
      str([t.get("step") for t in res2["decision_trace"]]))
check("慢环失败：有兜底回复，不抛异常", bool(res2["reply"]))

# 预算跨请求不累积
res3 = agent.decide("打开客厅灯")
check("连续调用：预算按请求重置", res3["llm_budget"]["calls"] == 0,
      str(res3["llm_budget"]))

# ---------------------------------------------------------------- 汇总
print()
print("=" * 60)
total = PASSED + FAILED
print(f"  结果: {PASSED} passed, {FAILED} failed  (共 {total} 项)")
print("=" * 60)

sys.exit(1 if FAILED else 0)
