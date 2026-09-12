# -*- coding: utf-8 -*-
"""能力注册表 + 事件路由 演示脚本（python demo_capability_route.py）。

5 场景：
  S1 口语别名解析（「好热」「太亮」→ 能力契约）
  S2 可执行数字孪生（虚拟吊灯 ↔ Matter 占位适配器，同契约 + 回读一致性）
  S3 跌倒事件 → 事件路由 → 开灯（confirmed + 回读证据 + 数字人告警）
  S4 冷却生效（60s 内重复跌倒事件只告警一次）
  S5 证据链（回读确认 + 孪生一致性标记）
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from backend.devices.registry import DeviceRegistry
from backend.capabilities import CapabilityRegistry
from backend.event_routes import EventRouter


class DemoManager:
    """演示用广播（打印代替 WebSocket）。"""

    async def broadcast(self, message: dict):
        print(f"      [数字人播报] {message.get('reply', '')[:60]}")


def hr(title):
    print(f"\n{'=' * 64}\n  {title}\n{'=' * 64}")


# ============ 初始化 ============
registry = DeviceRegistry()
caps = CapabilityRegistry()
manager = DemoManager()
router = EventRouter(registry, manager)

hr("S1 口语别名解析：自然语言 → 能力契约")
for text in ["我好热啊", "这屋太亮了", "拉上窗帘", "风大点", "太干了"]:
    r = caps.resolve(text)
    if r:
        print(f"  「{text}」→ {r['capability']} | command={r['command']} | params={r['params']}")
        devices = caps.find_devices(r["capability"], registry)
        print(f"      契约可驱动的设备: {[d.device_id for d in devices][:3]}")
    else:
        print(f"  「{text}」→ 未命中能力契约")

hr("S2 可执行数字孪生：同一契约驱动虚实两侧")
# 建立孪生对：虚拟吊灯（数字孪生沙盒）↔ Matter 实体占位适配器
caps.pair_twin("light_living_01", "matter_light_01", name="Matter 实体吊灯", protocol="matter")

def twin_sync(device, attrs):
    twin = caps.get_twin(device.device_id)
    if twin is None:
        return None
    twin.apply(attrs)
    return twin.readback(attrs)

registry.set_twin_sync(twin_sync)
result = registry.control("light_living_01", "set_brightness", {"brightness": 60})
print(f"  虚拟侧命令: set_brightness 60 → status={result['status']}")
print(f"  虚拟侧回读: {result['readback']}")
print(f"  实体侧证据: {result['twin']}")

hr("S3 跌倒事件 → 事件路由 → 开灯 + 数字人告警")
event = {"type": "fall_detected", "source": "edge_vision",
         "payload": {"severity": "critical", "summary": "端侧视觉检测到有人跌倒"}}
ev = router.route(event)
print(f"  命中路由: {ev.matched_routes}")
for action in ev.actions:
    if action.get("type") == "device":
        print(f"  设备动作: {action.get('device_id')}.{action.get('command')} → status={action.get('status')} readback={action.get('readback')}")
    elif action.get("type") == "alert":
        print(f"  告警消息: {action.get('message')}")

hr("S4 冷却生效：60s 内重复跌倒事件只执行一次")
ev2 = router.route({"type": "fall_detected", "source": "edge_vision",
                    "payload": {"severity": "critical"}})
for action in ev2.actions:
    if action.get("type") == "cooldown":
        print(f"  冷却中跳过: {action['detail']}")
    else:
        print(f"  动作: {action.get('type')} status={action.get('status')}")

hr("S5 证据链：回读确认 + 孪生一致性")
lights = registry.get("light_living_01")
print(f"  虚拟侧最终状态: brightness={lights.get('brightness')} on={lights.get('on')}")
twin = caps.get_twin("light_living_01")
print(f"  实体侧最终状态: {twin.state}")
# 以虚拟侧当前状态为预期，验证实体侧实测态与孪生体一致
expected = {"on": lights.get("on"), "brightness": lights.get("brightness"),
            "color_temp": lights.get("color_temp")}
print(f"  孪生一致性: {twin.readback(expected)['confirmed']}（虚拟预期态 ↔ 实体实测态逐项比对）")

print("\n演示完成。")
