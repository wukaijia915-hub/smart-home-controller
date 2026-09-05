# -*- coding: utf-8 -*-
"""端云协同 + 多模态 演示脚本。

跑一遍命题最看重的几个场景，直观展示「数据分级 → 端/云路由 → 端侧小模型/云侧大模型」闭环：
  S1 端侧视觉跌倒检测（L0 高敏，画面本地处理，绝不上云）
  S2 端侧高频指令（开灯，本地规则 <100ms）
  S3 端侧隐私敏感指令（看摄像头，L0 强制本地，即使有 LLM 也不上云）
  S4 中敏数据脱敏（环境/设备状态，L1 仅上传脱敏摘要）
  S5 云端复杂规划（多设备联动，L2 上云；未配置 LLM 时走端侧兜底）

用法：python demo_edge_cloud.py
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from backend.devices.registry import DeviceRegistry
from backend.agent import HomeAgent
from backend.edge_cloud import DecisionRouter, PrivacyGuard
from backend.multimodal import VisionEventDetector, MultimodalContext


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


reg = DeviceRegistry()
agent = HomeAgent(reg)
router = agent.router
detector = VisionEventDetector()

hr("S1 · 端侧视觉跌倒检测（L0 高敏，画面本地处理，绝不上云）")
event = detector.detect("老人跌倒")
alert = detector.event_to_alert(event)
print("视觉事件:", event.to_dict())
print("主动告警:", alert)

hr("S2 · 端侧高频指令（本地规则，<100ms）")
t0 = time.perf_counter()
r = agent.decide("打开客厅灯")
dt = (time.perf_counter() - t0) * 1000
print(f"指令「打开客厅灯」 → {r['reply']}")
print(f"动作: {[a['device_id'] + '.' + a['command'] for a in r['actions']]}")
print(f"端云路由: {r['edge_cloud']}")
print(f"耗时: {dt:.1f} ms（端侧本地推理，未上云）")

hr("S3 · 端侧隐私敏感指令（L0，强制本地，绝不调用云端 LLM）")
for t in ["看看客厅摄像头拍到了谁", "帮我记录一下今天的睡眠情况", "把录像保存到云端"]:
    route = router.route(t)
    r = agent.decide(t)
    print(f"「{t}」 → 分级={route.level} | 上云允许={route.cloud_allowed} | {route.reason}")
    print(f"   reply={r['reply'][:40]}")

hr("S4 · 中敏数据脱敏（L1，仅上传脱敏摘要）")
guard = agent.router.guard
raw = "家里温度多少度？我的电话是13812345678，家住北京海淀区中关村大街1号"
route = router.route(raw)
print(f"原文: {raw}")
print(f"分级={route.level} | 需脱敏={route.need_sanitize}")
print(f"脱敏后: {guard.sanitize(raw)}")

hr("S5 · 云端复杂规划（L2 上云；未配置 LLM 时走端侧兜底）")
text = "我今天要在家里办公，帮我把环境调得舒服一点"
route = router.route(text)
r = agent.decide(text)
print(f"指令「{text}」 → 分级={route.level} | 允许上云={route.cloud_allowed}")
print(f"LLM 是否已配置: {agent.llm.available}")
print(f"本次决策来源: {r.get('decision_trace', [{}])[0].get('detail', '')}")
print(f"reply={r['reply'][:50]}")
print(f"端云路由: {r['edge_cloud']}")

hr("上云数据审计（数据最小化）")
guard = agent.router.guard
print(f"累计上云次数: {guard.upload_count()}")
for e in guard.audit_snapshot():
    print(" -", e)

print("\n✅ 演示结束。以上 S1/S3 的高敏画面与数据始终在端侧本地处理，未上传云端。")
