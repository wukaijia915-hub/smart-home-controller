# -*- coding: utf-8 -*-
"""多后端大模型客户端（OpenAI 兼容协议）。
支持智谱 / DeepSeek / 通义千问等多后端，自动故障转移，复杂度感知路由。
未配置任何 API Key 时 available=False，由 Agent 走规则引擎兜底。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logger = logging.getLogger("llm_client")


def resolve_proxies() -> Optional[dict]:
    """本项目的模型端点一律直连，不走系统代理。

    为什么必须显式绕过：Windows 的系统代理常被设成本地代理端口
    （实测本机为 http://127.0.0.1:7897）。该代理未启动时 requests 抛
    ProxyError，而 chat_json 的 except 会把它吞成「返回 None」—— 表现为
    **LLM 层无声消失、系统静默降级到规则层**，界面上看不出任何异常，
    演示现场排查成本极高（实测确认：走代理 20-40ms 即失败，直连 0.9-1.9s 正常）。

    另外智谱/DeepSeek/通义等端点均为境内地址，本就不该经过境外代理；
    实测走代理时单次调用约 5-9s，直连约 1.4-3.3s，代理本身还在增加延迟。

    确实需要代理的场景（例如换成境外端点）用 `LLM_USE_SYSTEM_PROXY=1` 显式开启。
    """
    if os.getenv("LLM_USE_SYSTEM_PROXY", "").strip().lower() in ("1", "true", "yes"):
        return None      # 交回 requests 按环境变量/系统设置解析
    return {"http": None, "https": None}


def _parse_json_object(content: str) -> Optional[dict]:
    """把模型输出解析为 JSON **对象**。解析失败或不是对象一律返回 None。

    容忍两种常见的真实世界偏差：
    - 被 markdown 代码围栏包裹（```json ... ```）
    - 前后带解释性文字（截取第一个 { 到最后一个 }）
    """
    if not content:
        return None
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _log_call(duration_ms: float, ok: bool, backend: str, model: str,
              messages: list[dict], output_chars: int, kind: str,
              degrade_reason: str = "") -> None:
    """把一次模型调用记入观测层。观测失败绝不影响调用本身。"""
    try:
        from .observability.llm_log import llm_log
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages or [])
        llm_log.record_call(duration_ms=duration_ms, ok=ok, backend=backend,
                            model=model, prompt_chars=prompt_chars,
                            output_chars=output_chars, kind=kind,
                            degrade_reason=degrade_reason)
    except Exception:
        pass


# 预设后端配置（OpenAI 兼容协议）
PRESET_BACKENDS = {
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "env_key": "ZHIPU_API_KEY",
        "fast": True,  # 适合简单指令
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
        "fast": True,
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-turbo",
        "env_key": "DASHSCOPE_API_KEY",
        "fast": True,
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "env_key": "MOONSHOT_API_KEY",
        "fast": False,  # 适合复杂推理
    },
}


class LLMBackend:
    """单个 LLM 后端。"""
    def __init__(self, name: str, config: dict):
        self.name = name
        self.base_url = config["base_url"].rstrip("/")
        self.model = config["model"]
        self.api_key = os.getenv(config["env_key"], "")
        self.timeout = float(os.getenv("LLM_TIMEOUT", "8"))
        self.fast = config.get("fast", True)
        self.available = bool(self.api_key)
        self.fail_count = 0
        self.last_fail_time = 0
        self.circuit_breaker = 60  # 连续失败后冷却 60 秒
        self._warned = False       # 首次失败告警只发一次，避免刷屏

    def is_ready(self) -> bool:
        if not self.available:
            return False
        # 熔断：连续失败后冷却
        if self.fail_count >= 3 and time.time() - self.last_fail_time < self.circuit_breaker:
            self._warn_once(
                f"熔断器打开（连续失败 {self.fail_count} 次），"
                f"冷却 {self.circuit_breaker}s 内不再调用该后端")
            return False
        return True

    def _post(self, messages: list[dict], temperature: float, max_tokens: int,
              timeout: Optional[float], json_mode: bool, kind: str) -> Optional[str]:
        """发一次请求，返回原始文本内容；任何异常返回 None。

        日志记录放在这一层而不是各调用方：`chat_json` / `chat_text` 都经过它，
        因此"模型被调了几次、各花多久、成功与否"不会因为多了个新调用方式就漏计。
        """
        if not self.is_ready():
            _log_call(0.0, False, self.name, self.model, messages, 0, kind,
                      degrade_reason="后端不可用（无 Key 或熔断中）")
            return None
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        start = time.perf_counter()
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=timeout or self.timeout,
                proxies=resolve_proxies())
            if resp.status_code != 200:
                self._record_failure()
                self._warn_once(f"HTTP {resp.status_code}: {resp.text[:200]}")
                _log_call(_ms(start), False, self.name, self.model, messages, 0, kind,
                          degrade_reason=f"HTTP {resp.status_code}")
                return None
            content = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            self._record_failure()
            self._warn_once(f"{type(e).__name__}: {e}")
            _log_call(_ms(start), False, self.name, self.model, messages, 0, kind,
                      degrade_reason=f"{type(e).__name__}")
            return None
        _log_call(_ms(start), True, self.name, self.model, messages, len(content or ""), kind)
        self.fail_count = 0  # 成功重置失败计数
        return content

    def chat_json(self, messages: list[dict], temperature: float = 0.2,
                  max_tokens: int = 500, timeout: Optional[float] = None) -> Optional[dict]:
        """返回解析后的 JSON **对象**；任何异常情况一律返回 None。

        契约保证：返回值要么是 dict，要么是 None。
        （旧实现 `return json.loads(content)` 会把模型输出的 JSON 数组原样返回，
         调用方 manager_agent._llm_translate 对其调用 .get() 直接 AttributeError。
          即使请求了 response_format=json_object，模型也不保证遵守。）

        timeout 可覆盖 `LLM_TIMEOUT` 环境变量（默认 8s，实测被设为 30s，形同虚设）。
        """
        content = self._post(messages, temperature, max_tokens, timeout,
                             json_mode=True, kind="chat_json")
        if content is None:
            return None
        result = _parse_json_object(content)
        if result is None:
            # 传输正常但模型没按约定返回 JSON 对象：不算后端故障（不累加 fail_count），
            # 返回 None 让上层降级到结构化兜底路径。
            logger.warning(f"[{self.name}] 返回内容不是 JSON 对象，已降级")
            return None
        return result

    def chat_text(self, messages: list[dict], temperature: float = 0.4,
                  max_tokens: int = 800, timeout: Optional[float] = None) -> Optional[str]:
        """返回模型输出的**纯文本**（不做 JSON 约定）。

        日报生成走这条路：要求模型把中文段落包进 JSON 字符串里，等于让它多做一步
        容易出错的转义（换行、引号），而这一层的正确性没有护栏兜底 ——
        日报读起来怪，用户看到的就是"系统坏了"。纯文本路径没有这个风险。
        """
        content = self._post(messages, temperature, max_tokens, timeout,
                             json_mode=False, kind="chat_text")
        return content.strip() if content else None

    def _record_failure(self):
        self.fail_count += 1
        self.last_fail_time = time.time()

    def _warn_once(self, detail: str) -> None:
        """首次调用失败时告警一次。

        失败会被上层吞成「返回 None → 降级到规则层」，系统照常工作，
        所以 LLM 层其实已经不可用这件事，从接口返回和界面上都看不出来。
        演示现场最难排查的正是这种无声降级，因此至少要留下一条日志。
        """
        if not self._warned:
            self._warned = True
            logger.warning(
                f"[{self.name}] 模型调用失败，已降级到规则层"
                f"（同类失败后续不再重复告警）: {detail}")


class LLMClient:
    """多后端 LLM 客户端，支持自动故障转移和复杂度感知路由。

    环境变量：
    - `LLM_OFFLINE=1` 强制离线：不发起任何网络请求，全部走规则/结构化解。
      用于「断网演练」演示，以及让回归测试可复现（不依赖网络与模型输出波动）。
    """
    def __init__(self):
        self.offline = os.getenv("LLM_OFFLINE", "").strip() in ("1", "true", "True")
        self.backends: list[LLMBackend] = []
        self._init_backends()
        self.available = (not self.offline) and any(b.available for b in self.backends)
        self.last_used_backend = None

    def _init_backends(self):
        # 优先使用 .env 中显式配置的 LLM_BASE_URL（兼容旧配置）
        legacy_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        legacy_key = os.getenv("LLM_API_KEY", "")
        if legacy_url and legacy_key:
            self.backends.append(LLMBackend("legacy", {
                "base_url": legacy_url,
                "model": os.getenv("LLM_MODEL", "glm-4-flash"),
                "env_key": "LLM_API_KEY",
                "fast": True,
            }))
        # 加载预设后端
        for name, config in PRESET_BACKENDS.items():
            if os.getenv(config["env_key"]):
                self.backends.append(LLMBackend(name, config))

    def _estimate_complexity(self, text: str) -> str:
        """简单估算指令复杂度：simple / complex。"""
        # 长文本、多条件、多设备联动 → complex
        if len(text) > 30:
            return "complex"
        complex_keywords = ["同时", "并且", "然后", "如果", "因为", "帮我", "所有", "全部", "分别"]
        if any(k in text for k in complex_keywords):
            return "complex"
        return "simple"

    def _select_backends(self, text: str) -> list[LLMBackend]:
        """根据复杂度选择后端优先级。simple 优先 fast 后端，complex 优先强模型。"""
        complexity = self._estimate_complexity(text)
        ready = [b for b in self.backends if b.is_ready()]
        if complexity == "simple":
            # 快模型优先
            ready.sort(key=lambda b: (not b.fast, b.fail_count))
        else:
            # 复杂推理优先强模型（非 fast）
            ready.sort(key=lambda b: (b.fast, b.fail_count))
        return ready

    def chat_json(self, messages: list[dict], temperature: float = 0.2,
                  max_tokens: int = 500, context_text: str = "",
                  timeout: Optional[float] = None) -> Optional[dict]:
        """调用 LLM，自动故障转移。context_text 用于复杂度路由，timeout 可覆盖默认超时。"""
        if not self.available:
            return None
        candidates = self._select_backends(context_text)
        if not candidates:
            return None
        for backend in candidates:
            result = backend.chat_json(messages, temperature, max_tokens, timeout=timeout)
            if result is not None:
                self.last_used_backend = backend.name
                return result
        return None

    def chat_text(self, messages: list[dict], temperature: float = 0.4,
                  max_tokens: int = 800, context_text: str = "",
                  timeout: Optional[float] = None) -> Optional[str]:
        """调用 LLM 并返回纯文本。自动故障转移，全部失败返回 None。

        与 chat_json 同一套故障转移与观测：没有理由让"生成日报"这条路径
        脱离统一的失败可见性。
        """
        if not self.available:
            return None
        candidates = self._select_backends(context_text)
        for backend in candidates:
            content = backend.chat_text(messages, temperature, max_tokens, timeout=timeout)
            if content:
                self.last_used_backend = backend.name
                return content
        return None

    def get_status(self) -> dict:
        """返回各后端状态，用于调试和展示。"""
        return {
            "available": self.available,
            "offline": self.offline,
            "last_used": self.last_used_backend,
            "backends": [
                {
                    "name": b.name,
                    "model": b.model,
                    "available": b.available,
                    "ready": b.is_ready(),
                    "fail_count": b.fail_count,
                    "fast": b.fast,
                }
                for b in self.backends
            ],
        }
