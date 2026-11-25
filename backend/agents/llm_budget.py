# -*- coding: utf-8 -*-
"""慢环 LLM 预算：给一次 decide() 的模型调用设上界。

## 它解决什么问题

不是延迟优化 —— 已实测证伪（`experiments/README.md` 实验五：延迟由输出 token 数
决定，与调用次数、prompt 体积都无关）。**重构管线、压缩 prompt 都不降延迟。**

它解决的是**无界**问题：

1. `.env` 里 `LLM_TIMEOUT=30`，一次卡住的请求会把 `asyncio.to_thread` 的工作线程
   占用 30 秒。`decide()` 走线程池，多个并发请求会迅速耗光线程。
2. 慢环原本没有任何总时长上限。若将来（如阶段 4 的每日报告）在一次决策里调用多次
   模型，耗时会线性叠加而不受限。
3. 超时/失败是**静默**的：`chat_json` 返回 None，上层退到兜底回复，用户与日志里
   都看不出"这次其实降级了"。

## 设计取向

**失败关闭而非失败变慢**：预算耗尽或单次超时就立即降级到规则/兜底路径，
并在 `decision_trace` 打 `degraded` 标记，让降级可见。

预算按"一次 decide()"的粒度重置，由 `HomeAgent.decide()` 调用 `begin()`。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("llm_budget")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except (TypeError, ValueError):
        return default


@dataclass
class BudgetState:
    """一次决策周期内的预算使用情况（可序列化，供 decision_trace / 指标面板展示）。"""
    calls: int = 0
    elapsed_ms: int = 0
    degraded: bool = False
    reason: str = ""
    per_call_ms: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "calls": self.calls,
            "elapsed_ms": self.elapsed_ms,
            "degraded": self.degraded,
            "per_call_ms": self.per_call_ms,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


class LLMBudget:
    """一次 decide() 的模型调用预算（次数 / 墙钟 / 单次超时）。"""

    def __init__(self, max_calls: Optional[int] = None,
                 max_wall_seconds: Optional[float] = None,
                 per_call_timeout: Optional[float] = None):
        # 当前实现实际只用 1 次（Root 慢环），留余量给后续的外环/记忆检索等调用方
        self.max_calls = max_calls if max_calls is not None else _env_int("LLM_MAX_CALLS", 3)
        self.max_wall_seconds = (max_wall_seconds if max_wall_seconds is not None
                                 else _env_float("LLM_MAX_WALL_SECONDS", 20.0))
        # 覆盖 .env 的 LLM_TIMEOUT（实测被设为 30s）。实测单次调用 1.4–3.3s（简单）
        # 到 ~18s（输出多），15s 约为中位的 5 倍，既能兜住又能容下正常慢调用。
        self.per_call_timeout = (per_call_timeout if per_call_timeout is not None
                                 else _env_float("LLM_CALL_TIMEOUT", 15.0))
        self.state = BudgetState()
        self._started_at = 0.0

    # ---------- 周期 ----------
    def begin(self) -> None:
        """开始一次决策周期，重置计数。"""
        self.state = BudgetState()
        self._started_at = time.perf_counter()

    def _elapsed(self) -> float:
        return time.perf_counter() - self._started_at if self._started_at else 0.0

    # ---------- 准入 ----------
    def allow_call(self) -> tuple[bool, str]:
        """判断当前是否还允许发起一次模型调用。返回 (是否允许, 不允许的原因)。"""
        if self.state.calls >= self.max_calls:
            return False, f"已达调用次数上限 {self.max_calls}"
        if self._elapsed() >= self.max_wall_seconds:
            return False, f"已达墙钟预算 {self.max_wall_seconds:.0f}s"
        return True, "ok"

    def degrade(self, reason: str) -> None:
        """标记本次决策已降级（降级必须可见，否则和静默失败没区别）。"""
        self.state.degraded = True
        self.state.reason = reason
        logger.warning(f"慢环降级：{reason}")

    # ---------- 受预算约束的调用 ----------
    def call(self, llm, messages: list[dict], **kwargs) -> Optional[dict]:
        """在预算约束下调用 `llm.chat_json()`。

        超时用 `per_call_timeout` 覆盖后端默认值（`LLM_TIMEOUT`，实测为 30s）。
        返回 None 表示本次调用未成功或未发起，调用方应走降级路径。
        """
        allowed, why = self.allow_call()
        if not allowed:
            self.degrade(why)
            return None

        t0 = time.perf_counter()
        try:
            result = llm.chat_json(messages, timeout=self.per_call_timeout, **kwargs)
        except Exception as e:                      # 后端已吞异常，这里是最后一道兜底
            self.state.calls += 1
            self._record_call(t0)
            self.degrade(f"调用异常 {type(e).__name__}: {e}")
            return None

        self.state.calls += 1
        self._record_call(t0)

        if result is None:
            # chat_json 的契约是"失败返回 None"。失败原因可能是超时、熔断、
            # 网络错误或响应不是 JSON 对象 —— 后端都会各自告警，这里补一条
            # 决策层的记录，让"这次为什么没走 LLM"在 trace 里可查。
            self.degrade("模型调用未返回结果（超时/熔断/响应格式不符）")
        return result

    def _record_call(self, t0: float) -> None:
        ms = int((time.perf_counter() - t0) * 1000)
        self.state.per_call_ms.append(ms)
        self.state.elapsed_ms = int(self._elapsed() * 1000)

    # ---------- 展示 ----------
    def snapshot(self) -> dict:
        return self.state.to_dict()
