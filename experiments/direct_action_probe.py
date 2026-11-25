# -*- coding: utf-8 -*-
"""实验二：让模型直接输出具体设备动作，测量幻觉率。

背景：实验一证明 glm-4-flash 无法可靠产出批量翻译响应（多项 + 嵌套数组）。
      于是把 LLM 调用从 `1+N` 降到 `1` 的另一条路是：让 Root 直接输出
      {device_id, command, params}，Manager 完全不调 LLM，只做校验与执行。

这条路此前被排除的理由是"Root 提议具体 device_id 会引入幻觉"。
本实验就是为了把这个担忧量化：**幻觉率到底有多高？**

若幻觉率低，Manager 的 verify 足以兜住，设计就更简单（1 次 LLM 调用）；
若幻觉率高，则必须保留两级翻译。

只读实验，不写仓库文件。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import requests

from backend.capabilities import CapabilityRegistry
from backend.devices.registry import DeviceRegistry
from backend.llm_client import LLMClient, resolve_proxies


def build_prompt(registry) -> str:
    """把设备清单 + 命令 + 参数范围全部给模型。"""
    lines = []
    for d in registry.all():
        cmds = []
        for c in d.commands:
            attrs = []
            for pname, aname in PARAM_MAP.get(c, []):
                a = d.attributes.get(aname)
                if a is None:
                    continue
                if a.type == "enum" and a.options:
                    attrs.append(f"{pname}:{'|'.join(map(str, a.options))}")
                elif a.type == "number":
                    attrs.append(f"{pname}:{a.min}-{a.max}")
            cmds.append(f"{c}({','.join(attrs)})" if attrs else c)
        state = {k: v.value for k, v in d.attributes.items()}
        lines.append(f"- {d.device_id} | {d.name} | {d.room} | {d.category} | "
                     f"当前={json.dumps(state, ensure_ascii=False)} | 命令={' '.join(cmds)}")
    return (
        "你是智能家居中控的决策者。根据用户指令输出**具体的设备动作**。\n\n"
        '输出 JSON：{"is_command": true, "reply": "简短中文回复", '
        '"actions": [{"device_id": "...", "command": "...", "params": {...}}]}\n\n'
        "规则：\n"
        "1. device_id 与 command 必须严格取自下面清单，不得编造。\n"
        "2. 寒暄、提问、闲聊必须 is_command=false 且 actions 为空数组。\n"
        "3. 参数值必须在标注的范围内。\n"
        "4. 一个目标可能需要多条动作（如开空调到 24 度 = turn_on + set_temperature）。\n\n"
        "可用设备：\n" + "\n".join(lines) + "\n\n只输出 JSON。"
    )


# 命令 → (参数名, 属性名)，与 toolkit/schema_gen.py 保持一致
PARAM_MAP = {
    "set_brightness": [("brightness", "brightness")],
    "set_color_temp": [("color_temp", "color_temp")],
    "set_position": [("position", "position")],
    "set_temperature": [("temperature", "temperature")],
    "set_mode": [("mode", "mode")],
    "set_fan_speed": [("fan_speed", "fan_speed")],
    "set_volume": [("volume", "volume")],
    "set_speed": [("speed", "speed")],
    "set_target": [("target", "target")],
    "set_lock_mode": [("mode", "mode")],
    "set_clean_mode": [("mode", "clean_mode")],
    "set_fan_level": [("level", "fan_level")],
    "set_flame_level": [("level", "flame_level")],
}

# 这些输入在真实系统里到不了慢环（会被规则层命中），
# 所以只看真正会走慢环的模糊/多步指令
CASES = [
    "今天空气质量不太好，而且我感觉有点闷，帮我调整一下家里的环境",
    "我下午要在家办公，需要合适的光线和温度，顺便放点轻松的氛围",
    "孩子晚上要写作业，帮我设置一下适合学习的环境",
    "我要洗澡了，帮我把环境弄舒服点",
    "家里来客人了，准备一下",
]


def main() -> None:
    registry = DeviceRegistry()
    caps = CapabilityRegistry()
    caps.auto_pair(registry.all())
    client = LLMClient()
    if not client.available:
        print("无可用 LLM 后端")
        return
    backend = client.backends[0]
    system = build_prompt(registry)

    print("=" * 76)
    print("  实验二：Root 直接输出具体动作 —— 幻觉率测量")
    print("=" * 76)
    print(f"  模型 {backend.model} | system prompt {len(system)} 字符（≈{len(system)//1.6:.0f} token）")
    print()

    total_actions = 0
    hallucinated = 0
    bad_params = 0
    latencies = []
    non_dict = 0

    for text in CASES:
        t0 = time.perf_counter()
        resp = requests.post(
            f"{backend.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {backend.api_key}",
                     "Content-Type": "application/json"},
            json={"model": backend.model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": f"用户指令：{text}"}],
                  "temperature": 0.2, "max_tokens": 700,
                  "response_format": {"type": "json_object"}},
            timeout=30, proxies=resolve_proxies(),
        )
        dt = time.perf_counter() - t0
        latencies.append(dt)
        content = resp.json()["choices"][0]["message"]["content"]

        try:
            plan = json.loads(content)
        except json.JSONDecodeError:
            print(f"  ✗ JSON 解析失败：{text[:26]}")
            non_dict += 1
            continue
        if isinstance(plan, list):
            plan = plan[0] if plan and isinstance(plan[0], dict) else None
        if not isinstance(plan, dict):
            print(f"  ✗ 返回非对象：{text[:26]}  ({type(plan).__name__})")
            non_dict += 1
            continue

        actions = plan.get("actions") or []
        issues = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            total_actions += 1
            dev = registry.get(a.get("device_id", ""))
            if dev is None:
                hallucinated += 1
                issues.append(f"设备不存在:{a.get('device_id')}")
                continue
            cmd = a.get("command", "")
            if cmd not in dev.commands:
                hallucinated += 1
                issues.append(f"{dev.device_id}.{cmd} 命令非法")
                continue
            # 参数范围校验（Manager._verify + 租约包络将来做的事）
            for pname, aname in PARAM_MAP.get(cmd, []):
                if pname not in (a.get("params") or {}):
                    continue
                attr = dev.attributes.get(aname)
                if attr is None:
                    continue
                v = a["params"][pname]
                if attr.type == "number":
                    try:
                        n = float(v)
                    except (TypeError, ValueError):
                        bad_params += 1
                        issues.append(f"{cmd}.{pname}={v!r} 非数值")
                        continue
                    if (attr.min is not None and n < attr.min) or \
                       (attr.max is not None and n > attr.max):
                        bad_params += 1
                        issues.append(f"{cmd}.{pname}={v} 越界[{attr.min},{attr.max}]")
                elif attr.type == "enum" and attr.options and v not in attr.options:
                    bad_params += 1
                    issues.append(f"{cmd}.{pname}={v} 非法枚举")

        flag = "✓" if not issues else "✗"
        print(f"  {flag} {dt:5.1f}s  动作 {len(actions)} 条  is_command={plan.get('is_command')}  {text[:24]}")
        for it in issues:
            print(f"        └─ {it}")

    print()
    print("-" * 76)
    srt = sorted(latencies)
    print(f"  非对象返回: {non_dict}/{len(CASES)}")
    print(f"  动作总数: {total_actions}")
    print(f"  幻觉动作（设备/命令不存在）: {hallucinated}  "
          f"→ {hallucinated / total_actions * 100:.1f}%" if total_actions else "  无动作")
    print(f"  参数越界/非法: {bad_params}")
    print(f"  单次调用延迟: 平均 {sum(latencies)/len(latencies):.1f}s  "
          f"中位 {srt[len(srt)//2]:.1f}s  最大 {max(srt):.1f}s")
    print("-" * 76)


if __name__ == "__main__":
    main()
