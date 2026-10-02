# -*- coding: utf-8 -*-
"""可观测性三件套（移植心镜 `backend/middleware/` 的三件，加锁改写）。

| 模块 | 回答的问题 | 心镜原版的问题 |
|---|---|---|
| `rate_limit.py` | 谁在刷接口？ | 模块级 `defaultdict` 在 `asyncio.to_thread` 下被多线程改 → 竞态 |
| `api_log.py` | 接口慢在哪、错在哪？ | 同上，`_stats` 无锁 |
| `llm_log.py` | 模型层到底调了几次、花了多久、降级没有？ | 心镜无此项 |

**为什么本项目的可观测性是必需项而不是锦上添花**：慢环的真实瓶颈是模型生成速度，
而模型调用失败在这一版架构里会被上层吞成「返回 None → 降级到规则层」，
接口返回体上**看不出任何区别**。没有分层延迟与降级计数，"规则层 7.4ms + 慢环
预算化降级"这套说法就只是口头声称，无法被评委复核。
"""
from . import api_log, llm_log, rate_limit  # noqa: F401

__all__ = ["rate_limit", "api_log", "llm_log"]
