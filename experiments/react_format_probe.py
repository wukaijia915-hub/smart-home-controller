# -*- coding: utf-8 -*-
"""实验：验证国产模型对 prompt-based ReAct 工具调用格式的遵循度。

决定阶段 1b 的方案形态：
  - 遵循度好 → 做工具循环（ReAct）
  - 遵循度差 → 改成"单次调用输出结构化计划"

心镜的 base_agent._think_prompt_react 用的是 <tool_call>{"name":..,"arguments":..}</tool_call>
标签格式，但它那条路径在自身默认配置下从未被走到，**没有任何实测依据**。
本实验就是补上这个证据。

只读实验，不改动仓库任何文件，不写入任何日志。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend.llm_client import LLMClient

# ---------------------------------------------------------------- 被测的提示词

TOOLS_DESC = """- query_home_state(): 查询全屋设备当前状态与传感器读数
- list_devices(room): 列出指定房间的设备（room 可省略）
- list_commands(device_id): 查询某设备支持的命令与参数
- actuate(device_id, command, params): 执行设备动作（唯一有副作用的工具）"""

REACT_SYS = f"""你是智能家居中控的决策者。只提议、不执行；需要执行时通过工具请求。

## 工具使用说明

当需要使用工具时，输出：
<tool_call>
{{"name": "工具名称", "arguments": {{"参数名": "参数值"}} }}
</tool_call>

系统会执行工具并把结果返回，你基于结果继续推理。

## 当前可用工具

{TOOLS_DESC}

## 规则
1. 一次只请求一个工具调用
2. JSON 必须合法，参数名与工具定义一致
3. 最终答案用自然语言，不要包含 <tool_call> 标签
4. 不需要工具就直接回答"""


def parse_tool_call(text: str):
    """按心镜的解析逻辑提取工具调用。返回 (name, args) 或 None。"""
    m = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        # 裸 JSON 兜底（心镜的 fallback 分支）
        jm = re.search(r'\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\}',
                       text, re.DOTALL)
        if jm:
            raw = jm.group(0)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj.get("name"), obj.get("arguments", {})


# ---------------------------------------------------------------- 测试用例
# (标签, 用户输入, 期望行为)
CASES = [
    ("单工具调用", "帮我把客厅灯调暗一点", "call"),
    ("需先查状态", "现在家里什么情况？", "call"),
    ("多步链路", "把客厅空调调到 24 度", "call"),
    ("应直接回答", "你好呀，你是谁？", "direct"),
    ("应拒绝臆造", "帮我打开电磁炉", "either"),   # 工具表里没有的设备
]


def run(turns: int = 1):
    client = LLMClient()
    print("=" * 72)
    print("  prompt-based ReAct 格式遵循度实验")
    print("=" * 72)
    print(f"  LLM 可用: {client.available}  |  后端: {[b.name for b in client.backends]}")
    if not client.available:
        print("  ✗ 无可用后端，实验无法进行")
        return
    model = client.backends[0].model
    print(f"  模型: {model}")
    print()

    ok_format = 0
    total = 0
    latencies = []

    for label, user_text, expect in CASES:
        for trial in range(turns):
            messages = [{"role": "system", "content": REACT_SYS},
                        {"role": "user", "content": user_text}]
            t0 = time.perf_counter()
            try:
                # 用 chat_raw 语义：这里直接取原始文本，所以绕开 chat_json 的 JSON 强约束
                raw = _raw_chat(client, messages)
            except Exception as e:
                print(f"  [{label}] 调用异常: {type(e).__name__}: {e}")
                total += 1
                continue
            dt = time.perf_counter() - t0
            latencies.append(dt)
            total += 1

            call = parse_tool_call(raw)
            has_tag = "<tool_call>" in raw

            if expect == "call":
                good = call is not None
            elif expect == "direct":
                good = call is None
            else:
                good = True     # 两种都可接受
            if good:
                ok_format += 1

            mark = "✓" if good else "✗"
            if call:
                shape = f"<tool_call> name={call[0]} args={json.dumps(call[1], ensure_ascii=False)[:52]}"
            elif has_tag:
                shape = "<tool_call> 标签存在但 JSON 解析失败"
            else:
                shape = "无标签（直接回答）"
            print(f"  {mark} [{label}] {dt:5.1f}s  {shape}")
            if not good:
                print(f"      ── 原始输出前 200 字: {raw[:200]!r}")

    print()
    print("-" * 72)
    if latencies:
        srt = sorted(latencies)
        print(f"  格式正确率: {ok_format}/{total} = {ok_format / total * 100:.0f}%")
        print(f"  延迟: 平均 {sum(latencies) / len(latencies):.1f}s  "
              f"中位 {srt[len(srt) // 2]:.1f}s  最大 {max(srt):.1f}s")
    print("-" * 72)


def _raw_chat(client: LLMClient, messages: list[dict]) -> str:
    """取模型原始文本输出（不经 JSON 解析），用于观察格式遵循情况。

    显式绕过系统代理：本机 Windows 系统代理指向 127.0.0.1:7897（Clash 类），
    代理未启动时 requests 会直接 ProxyError；且 open.bigmodel.cn 是境内接口，
    本就不该走代理。
    """
    import requests
    backend = client._select_backends("test")[0]
    resp = requests.post(
        f"{backend.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {backend.api_key}",
                 "Content-Type": "application/json"},
        json={"model": backend.model, "messages": messages,
              "temperature": 0.2, "max_tokens": 400},
        timeout=backend.timeout,
        proxies={"http": None, "https": None},
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


if __name__ == "__main__":
    run(turns=int(sys.argv[1]) if len(sys.argv) > 1 else 1)
