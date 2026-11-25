# -*- coding: utf-8 -*-
"""模型调用日志 + 决策分层延迟。这是本项目"数字说话"的取数来源。

为什么需要它（而心镜没有这一项）：

1. **慢环延迟必须分层报告**。一次 `decide()` 里，规则层是纯内存查表（毫秒级），
   慢环是模型生成（秒级）。把两者混成一个"平均响应时间"会得出毫无意义的数字，
   而分开报告才说得清：规则层覆盖标准场景集 100%，慢环走预算化降级、P95 有界。
   `/api/metrics/llm` 直接返回这两个分层的 avg/p95。
2. **降级必须可见**。模型调用失败会被上层吞成「返回 None → 降级到规则层」，
   接口返回体看不出区别。这里记录**降级率**，让"断网演练"有可展示的证据。
3. **token 用量**。延迟 ≈ 1.2s + 输出 token ÷ 15（见 `experiments/README.md`
   实验五）。记录输出长度才能解释延迟波动，也才知道优化该往哪走。

落盘 `data/llm_calls.jsonl`（逐次调用），内存保留最近 N 条样本用于算分位数。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

from ..config_loader import data_path

CALL_LOG_PATH = data_path("llm_calls.jsonl")
MAX_SAMPLES = 500


def percentile(values: list[float], pct: float) -> float:
    """最近秩法（nearest-rank）分位数。样本为空返回 0。

    刻意不用线性插值：样本量只有几十条时，插值出来的 p95 是一个**不存在的
    观测值**，而被问到时无法指着某一次调用来解释它。最近秩法给出的永远
    是真实出现过的数。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return round(ordered[k], 2)


class LLMLog:
    def __init__(self, path: Path | None = None, enabled: bool = True):
        self.path = path or CALL_LOG_PATH
        self.enabled = enabled
        self._lock = threading.Lock()
        self._calls: deque[dict] = deque(maxlen=MAX_SAMPLES)
        self._decisions: deque[dict] = deque(maxlen=MAX_SAMPLES)
        self._counters = {"calls": 0, "ok": 0, "failed": 0, "degraded": 0,
                          "prompt_tokens": 0, "output_tokens": 0}

    # ---------- 逐次模型调用 ----------
    def record_call(self, duration_ms: float, ok: bool, backend: str = "",
                    model: str = "", prompt_chars: int = 0, output_chars: int = 0,
                    kind: str = "chat_json", degrade_reason: str = "") -> None:
        prompt_tokens = _approx_tokens(prompt_chars)
        output_tokens = _approx_tokens(output_chars)
        with self._lock:
            self._counters["calls"] += 1
            self._counters["ok" if ok else "failed"] += 1
            self._counters["prompt_tokens"] += prompt_tokens
            self._counters["output_tokens"] += output_tokens
            self._calls.append({
                "ts": time.time(), "duration_ms": round(duration_ms, 1), "ok": ok,
                "backend": backend, "model": model, "kind": kind,
                "prompt_tokens": prompt_tokens, "output_tokens": output_tokens,
                "degrade_reason": degrade_reason,
            })
        self._append({"type": "llm_call", "duration_ms": round(duration_ms, 1), "ok": ok,
                      "backend": backend, "model": model, "kind": kind,
                      "prompt_tokens": prompt_tokens, "output_tokens": output_tokens,
                      "degrade_reason": degrade_reason})

    # ---------- 每次决策的分层延迟 ----------
    def record_decision(self, source: str, parse_ms: float, total_ms: float,
                        actions: int = 0, degraded: bool = False,
                        degrade_reason: str = "") -> None:
        """source: rule / llm / fallback。

        **两个延迟量必须都记，且名字不能混**：
        - `parse_ms` —— 漏斗 ①~⑤ 的**纯解析**耗时（亚毫秒级）
        - `total_ms` —— `decide()` 的**端到端**耗时（含设备执行、回读、审计写入）

        之前只记了 `parse_ms` 却把它当作"规则层延迟"对外报，而 `docs/BENCH.md`
        的"规则层延迟"是端到端口径 —— 同一句话在接口与文档里指向两个差两个数量级的
        数字（0.06ms vs 8.5ms）。这种同名不同义最难查，因为两边单独看都对。

        对外报延迟一律用**端到端**：用户感受到的是它。
        """
        with self._lock:
            if degraded:
                self._counters["degraded"] += 1
            self._decisions.append({
                "ts": time.time(), "source": source,
                "parse_ms": round(parse_ms, 3), "total_ms": round(total_ms, 2),
                "actions": actions, "degraded": degraded,
            })

    # ---------- 分层指标 ----------
    def metrics(self, budget_snapshot: dict | None = None) -> dict:
        with self._lock:
            calls = list(self._calls)
            decisions = list(self._decisions)
            counters = dict(self._counters)

        # 只有 source != "llm" 的样本进入"规则层"统计。fallback（空输入/条件句/
        # 未命中且未上云）同样在 plan() 内同步返回、同样没碰模型，归入规则层是
        # 正确口径，不是污染。
        rule = [d for d in decisions if d["source"] != "llm"]
        slow = [d for d in decisions if d["source"] == "llm"]
        rule_total = [d["total_ms"] for d in rule]
        rule_parse = [d["parse_ms"] for d in rule]
        slow_total = [d["total_ms"] for d in slow]
        call_ms = [c["duration_ms"] for c in calls]
        slow_calls = [c["duration_ms"] for c in calls if c["ok"] and c["kind"] == "chat_json"]

        return {
            "rule_path": {
                "samples": len(rule),
                # avg/p95/max 是**端到端**口径（含执行与审计）——
                # 这是用户实际感受到、也是 docs/BENCH.md 对外报的数字
                "avg_ms": round(sum(rule_total) / len(rule_total), 2) if rule_total else 0,
                "p95_ms": percentile(rule_total, 95),
                "max_ms": round(max(rule_total), 2) if rule_total else 0,
                "parse": {
                    "avg_ms": round(sum(rule_parse) / len(rule_parse), 3) if rule_parse else 0,
                    "p95_ms": percentile(rule_parse, 95),
                    "max_ms": round(max(rule_parse), 3) if rule_parse else 0,
                },
                "note": "avg/p95 为 decide() 端到端（含执行与审计），parse 为漏斗 "
                        "①~⑤ 纯解析。两者差两个数量级，报延迟一律用端到端口径",
            },
            "slow_path": {
                "samples": len(slow),
                "avg_ms": round(sum(slow_total) / len(slow_total), 1) if slow_total else 0,
                "p95_ms": percentile(slow_total, 95),
                "max_ms": round(max(slow_total), 1) if slow_total else 0,
                "note": "延迟 ≈ 1.2s + 输出 token ÷ 15；受模型生成速度主导，"
                        "与调用次数/prompt 体积无关（见 experiments/README.md 实验五）",
            },
            "llm_calls": {
                **counters,
                "success_rate": round(counters["ok"] / max(counters["calls"], 1), 3),
                "degrade_rate": round(counters["degraded"] / max(len(decisions), 1), 3),
                "avg_ms": round(sum(call_ms) / len(call_ms), 1) if call_ms else 0,
                "p95_ms": percentile(call_ms, 95),
                "ok_avg_ms": round(sum(slow_calls) / len(slow_calls), 1) if slow_calls else 0,
                "note": "prompt/output token 为按字符数估算，非服务商计费值",
            },
            "llm_budget": budget_snapshot or {},
            "source_distribution": _count_by(decisions, "source"),
        }

    def recent_calls(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._calls)[-limit:]

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
            self._decisions.clear()
            for k in self._counters:
                self._counters[k] = 0

    def _append(self, entry: dict) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entry["ts"] = entry.get("ts") or time.time()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _approx_tokens(chars: int) -> int:
    """按字符数粗估 token。中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token，
    这里统一取 chars / 1.6 作为折中。**是估算值**，接口返回里如实标注。"""
    return int(chars / 1.6) if chars else 0


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[str(r.get(key, ""))] = out.get(str(r.get(key, "")), 0) + 1
    return out


llm_log = LLMLog()
