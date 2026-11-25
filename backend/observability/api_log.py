# -*- coding: utf-8 -*-
"""API 调用统计：端点分布 / 状态码分布 / 平均延迟 / 最近错误。

**与心镜原版的差别：加锁。** 心镜的 `_stats` 是模块级 `defaultdict`，
而 FastAPI 的同步端点跑在 `asyncio.to_thread` 的工作线程池里，多个请求会并发
改同一批计数器。CPython 的 GIL 让单次 `+= 1` 侥幸不会崩，但
`_stats["recent_latency"].append()` 与随后的 `pop(0)` 在并发下会错位 ——
表现为"平均延迟忽高忽低"，且**无法复现**。本版读写都在锁内。

落盘为 JSONL（`data/api_access.jsonl`）而非心镜的纯文本行，便于直接过滤分析。
日志文件不参与演示，只用于事后复盘；写失败绝不影响请求本身。
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import date, datetime

from ..config_loader import data_path

ACCESS_LOG_PATH = data_path("api_access.jsonl")
MAX_LATENCY_SAMPLES = 200
MAX_RECENT_ERRORS = 20
MAX_ENDPOINT_KEYS = 200      # 防端点爆炸：非预期路径（扫描器）不应无限增长内存


class ApiLog:
    def __init__(self, path=None, enabled: bool = True):
        self.path = path or ACCESS_LOG_PATH
        self.enabled = enabled
        self._lock = threading.Lock()
        self._stats = {
            "total_requests": 0,
            "total_errors": 0,
            "endpoints": defaultdict(int),
            "daily_counts": defaultdict(int),
            "status_counts": defaultdict(int),
            "recent_latency": [],
            "recent_errors": [],
        }

    def record(self, method: str, path: str, status: int, duration_ms: float,
               client_ip: str = "", error: str = "") -> None:
        with self._lock:
            s = self._stats
            s["total_requests"] += 1
            if len(s["endpoints"]) < MAX_ENDPOINT_KEYS or path in s["endpoints"]:
                s["endpoints"][path] += 1
            s["daily_counts"][str(date.today())] += 1
            s["status_counts"][str(status)] += 1
            if status >= 400:
                s["total_errors"] += 1
            s["recent_latency"].append(duration_ms)
            del s["recent_latency"][:-MAX_LATENCY_SAMPLES]
            if error:
                s["recent_errors"].append({
                    "path": path, "method": method,
                    "error": error[:200], "at": datetime.now().isoformat(timespec="seconds"),
                })
                del s["recent_errors"][:-MAX_RECENT_ERRORS]
        if self.enabled:
            self._append_line(method, path, status, duration_ms, client_ip)

    def _append_line(self, method, path, status, duration_ms, client_ip) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entry = {"ts": round(time.time(), 3), "method": method, "path": path,
                     "status": status, "duration_ms": duration_ms, "ip": client_ip}
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # 观测层故障不得影响被观测的请求
            pass

    def stats(self) -> dict:
        with self._lock:
            s = self._stats
            lat = list(s["recent_latency"])
            return {
                "total_requests": s["total_requests"],
                "total_errors": s["total_errors"],
                "error_rate": round(s["total_errors"] / max(s["total_requests"], 1), 4),
                "top_endpoints": sorted(
                    ({"endpoint": k, "count": v} for k, v in s["endpoints"].items()),
                    key=lambda x: x["count"], reverse=True)[:10],
                "status_distribution": dict(s["status_counts"]),
                "daily_counts": dict(sorted(s["daily_counts"].items(), reverse=True)[:30]),
                "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else 0,
                "recent_errors": list(s["recent_errors"][-10:]),
            }

    def reset(self) -> None:
        with self._lock:
            self._stats = {
                "total_requests": 0, "total_errors": 0,
                "endpoints": defaultdict(int), "daily_counts": defaultdict(int),
                "status_counts": defaultdict(int),
                "recent_latency": [], "recent_errors": [],
            }


api_log = ApiLog()


def _register_middleware(app, log: ApiLog | None = None) -> None:
    """把 API 统计挂到 FastAPI 应用上。"""
    from starlette.middleware.base import BaseHTTPMiddleware

    target = log or api_log

    class ApiLoggingMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            start = time.perf_counter()
            status, err = 500, ""
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                raise
            finally:
                target.record(
                    request.method, request.url.path, status,
                    round((time.perf_counter() - start) * 1000, 2),
                    request.client.host if request.client else "",
                    err,
                )

    app.add_middleware(ApiLoggingMiddleware)
