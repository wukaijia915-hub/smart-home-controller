# -*- coding: utf-8 -*-
"""多后端大模型客户端（OpenAI 兼容协议）。
支持智谱 / DeepSeek / 通义千问等多后端，自动故障转移，复杂度感知路由。
未配置任何 API Key 时 available=False，由 Agent 走规则引擎兜底。
"""
from __future__ import annotations

import json
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

    def is_ready(self) -> bool:
        if not self.available:
            return False
        # 熔断：连续失败后冷却
        if self.fail_count >= 3 and time.time() - self.last_fail_time < self.circuit_breaker:
            return False
        return True

    def chat_json(self, messages: list[dict], temperature: float = 0.2,
                  max_tokens: int = 500) -> Optional[dict]:
        if not self.is_ready():
            return None
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code != 200:
                self._record_failure()
                return None
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)
            self.fail_count = 0  # 成功重置失败计数
            return result
        except Exception:
            self._record_failure()
            return None

    def _record_failure(self):
        self.fail_count += 1
        self.last_fail_time = time.time()


class LLMClient:
    """多后端 LLM 客户端，支持自动故障转移和复杂度感知路由。"""
    def __init__(self):
        self.backends: list[LLMBackend] = []
        self._init_backends()
        self.available = any(b.available for b in self.backends)
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
                  max_tokens: int = 500, context_text: str = "") -> Optional[dict]:
        """调用 LLM，自动故障转移。context_text 用于复杂度路由。"""
        if not self.available:
            return None
        candidates = self._select_backends(context_text)
        if not candidates:
            return None
        for backend in candidates:
            result = backend.chat_json(messages, temperature, max_tokens)
            if result is not None:
                self.last_used_backend = backend.name
                return result
        return None

    def get_status(self) -> dict:
        """返回各后端状态，用于调试和展示。"""
        return {
            "available": self.available,
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
