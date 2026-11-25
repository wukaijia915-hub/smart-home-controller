# -*- coding: utf-8 -*-
"""请求限流中间件：IP 级滑动窗口。

两档阈值：
- **普通端点 300 次/分钟**。不能按常见的 60/min 卡 —— 前端是 3D 孪生沙盒，
  页面加载要拉 three.module.js + 10 个 GLB 模型 + 若干 JS/CSS，单个 IP 一次
  刷新就是几十个请求；再加上每 10 秒一次的 `/api/perception` 轮询。
  按 60/min 会造成"演示时页面刷不出来"这种自伤。
- **LLM 端点（`/api/chat` 等）10 次/分钟**。这些端点会真的花钱、真的排队。

**与心镜原版的差别：加锁。** 心镜用模块级 `defaultdict(list)` 记录窗口，
而 FastAPI 里同步端点跑在 `asyncio.to_thread` 的工作线程池上 —— 多线程同时
`append`/`pop` 同一个 list 不是原子操作，窗口记录会错乱（表现为偶发误限流）。
本版所有读写都在 `threading.Lock` 内。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

# 普通端点阈值。见模块头说明：这是按"3D 前端一次刷新几十个请求"定的。
MAX_REQUESTS_PER_MINUTE = 300
# LLM 端点阈值（真花钱的路径）
LLM_MAX_REQUESTS_PER_MINUTE = 10
WINDOW_SECONDS = 60.0

# 会调用大模型的端点。前缀匹配，便于覆盖 /api/chat 之类无参数路径。
LLM_ENDPOINT_PREFIXES = (
    "/api/chat",
    "/api/external/command",
    "/api/jobs/daily_summary/run",   # 日报生成：LLM 优先
)
# 豁免路径：健康检查与静态资源不应被限流（限了就等于限自己的页面）
EXEMPT_PREFIXES = ("/mcp/health", "/docs", "/openapi.json")


class RateLimiter:
    def __init__(self, max_per_minute: int = MAX_REQUESTS_PER_MINUTE,
                 llm_max_per_minute: int = LLM_MAX_REQUESTS_PER_MINUTE):
        self.max_per_minute = max_per_minute
        self.llm_max_per_minute = llm_max_per_minute
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._llm_windows: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()
        self.rejected = 0

    def is_llm_endpoint(self, path: str) -> bool:
        return any(path.startswith(p) for p in LLM_ENDPOINT_PREFIXES)

    def is_exempt(self, path: str) -> bool:
        return any(path.startswith(p) for p in EXEMPT_PREFIXES)

    def check(self, ip: str, path: str, now: float | None = None) -> tuple[bool, int]:
        """返回 (是否放行, 当前窗口内已用次数)。"""
        if self.is_exempt(path):
            return True, 0
        now = time.time() if now is None else now
        llm = self.is_llm_endpoint(path)
        windows = self._llm_windows if llm else self._windows
        limit = self.llm_max_per_minute if llm else self.max_per_minute
        with self._lock:
            window = windows[ip]
            cutoff = now - WINDOW_SECONDS
            # 原地裁剪：保留窗口内的时间戳
            window[:] = [t for t in window if t >= cutoff]
            if len(window) >= limit:
                self.rejected += 1
                return False, len(window)
            window.append(now)
            return True, len(window)

    def retry_after(self, ip: str, path: str, now: float | None = None) -> int:
        """建议的等待秒数（最老那次请求滑出窗口所需时间）。"""
        now = time.time() if now is None else now
        windows = self._llm_windows if self.is_llm_endpoint(path) else self._windows
        with self._lock:
            window = windows.get(ip) or []
            if not window:
                return 0
            return max(1, int(WINDOW_SECONDS - (now - window[0])) + 1)

    def stats(self) -> dict:
        with self._lock:
            return {
                "limit_per_minute": self.max_per_minute,
                "llm_limit_per_minute": self.llm_max_per_minute,
                "tracked_ips": len(self._windows),
                "rejected_total": self.rejected,
            }

    def reset(self) -> None:
        """清空窗口。仅供测试使用（生产不调用）。"""
        with self._lock:
            self._windows.clear()
            self._llm_windows.clear()
            self.rejected = 0


limiter = RateLimiter()


def _register_middleware(app) -> None:
    """把限流挂到 FastAPI 应用上。在 main.py 里显式调用（不在导入期自动挂）。"""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class RateLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            ip = request.client.host if request.client else "unknown"
            path = request.url.path
            allowed, _used = limiter.check(ip, path)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={
                        "ok": False,
                        "error": "请求过于频繁，请稍后再试",
                        "retry_after_seconds": limiter.retry_after(ip, path),
                    },
                    headers={"Retry-After": str(limiter.retry_after(ip, path))},
                )
            return await call_next(request)

    app.add_middleware(RateLimitMiddleware)
