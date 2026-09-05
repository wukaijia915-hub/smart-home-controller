# -*- coding: utf-8 -*-
"""端云协同 + 多模态 模块测试（standalone，python tests_edge_cloud.py）。

覆盖：隐私分级、脱敏、端云路由、端侧视觉检测、上云审计、HomeAgent 端云集成。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from backend.devices.registry import DeviceRegistry
from backend.agent import HomeAgent
from backend.edge_cloud import DecisionRouter, PrivacyGuard, PrivacyLevel
from backend.multimodal import VisionEventDetector, MultimodalContext

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


print("== 隐私分级 ==")
g = PrivacyGuard()
check("摄像头→L0", g.classify("打开摄像头看看") == PrivacyLevel.L0)
check("健康→L0", g.classify("记录我的血压心率") == PrivacyLevel.L0)
check("跌倒→L0", g.classify("检测老人跌倒") == PrivacyLevel.L0)
check("语音通道→L0", g.classify("随便", modalities=["voice"]) == PrivacyLevel.L0)
check("视觉通道→L0", g.classify("随便", modalities=["vision"]) == PrivacyLevel.L0)
check("环境→L1", g.classify("客厅温度多少") == PrivacyLevel.L1)
check("纯指令→L2", g.classify("打开客厅灯") == PrivacyLevel.L2)

print("== 脱敏 ==")
san = g.sanitize("电话13812345678，住北京海淀区中关村大街1号")
check("手机号脱敏", "13812345678" not in san and "1**********" in san)
check("地址脱敏", "中关村大街" not in san)

print("== 端云路由 ==")
router = DecisionRouter()
r0 = router.route("打开摄像头录像", modalities=["vision"])
check("L0→edge_only", r0.target == "edge_only" and r0.cloud_allowed is False)
r1 = router.route("家里温度多少度")
check("L1→需脱敏", r1.need_sanitize is True and r1.cloud_allowed is True)
r2 = router.route("开灯")
check("L2→edge_first", r2.target == "edge_first" and r2.need_sanitize is False)

print("== 端侧视觉检测 ==")
det = VisionEventDetector()
ev = det.detect("老人跌倒")
check("跌倒检测命中", ev is not None and ev.event_type == "fall_detected")
check("画面本地处理", ev is not None and ev.local_processed and ev.privacy_level == PrivacyLevel.L0)
alert = det.event_to_alert(ev)
check("跌倒触发告警", alert is not None and alert["level"] == "critical")

print("== 多模态上下文 ==")
ctx = MultimodalContext().add_text("开灯").add_vision(ev).add_sensors({"temp_indoor": 25})
check("多模态聚合", set(ctx.modalities) == {"text", "vision", "sensor"} and len(ctx.vision_events) == 1)

print("== HomeAgent 端云集成 ==")
reg = DeviceRegistry()
agent = HomeAgent(reg)
r = agent.decide("打开客厅灯")
check("结果含 edge_cloud 路由", "edge_cloud" in r and "level" in r["edge_cloud"])
check("正常指令可决策", len(r["actions"]) >= 1)
# L0 高敏指令：路由必须禁止上云
r0 = agent.decide("打开摄像头录像")
check("L0 指令禁止上云", r0["edge_cloud"]["cloud_allowed"] is False)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
