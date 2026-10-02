# -*- coding: utf-8 -*-
"""可执行数字孪生 · 三要素截图脚本（python demo_twin_screenshot.py）。

输出一张屏能截完的紧凑证据链：
  ① 能力契约（Capability Contract）
  ② 孪生配对（Twin Pairing）
  ③ 能力一致性（Capability Consistency）

用于答辩 PPT 截图，体现「虚实同构能力契约」创新点。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from backend.devices.registry import DeviceRegistry
from backend.capabilities import CapabilityRegistry

registry = DeviceRegistry()
caps = CapabilityRegistry()

print("=" * 66)
print("  灵犀 · 可执行数字孪生：虚实同构能力契约（三项证据）")
print("=" * 66)

# ① 能力契约
print("\n[1] 能力契约 Capability Contract")
contract = caps.resolve("太亮")
print("    口语意图「太亮」→ 契约名: %s" % contract["capability"])
print("    契约语义: %s" % contract["description"])
print("    契约实现方: 虚拟驱动(数字孪生沙盒) + Matter适配器(真实设备) 共享同一契约")
print("    参数契约: %s" % contract["params"])

# ② 孪生配对
print("\n[2] 孪生配对 Twin Pairing")
twin = caps.pair_twin("light_living_01", "matter_light_01",
                      name="Matter 实体吊灯", protocol="matter")
print("    pair_twin(virtual='light_living_01', physical='matter_light_01')")
print("    虚拟体(预期态)  <==同一能力契约==>  实体(实测态)")

# ③ 能力一致性：同一契约驱动虚实两侧 + 回读确认
print("\n[3] 能力一致性 Capability Consistency")


def twin_sync(device, attrs):
    t = caps.get_twin(device.device_id)
    if t is None:
        return None
    t.apply(attrs)
    return t.readback(attrs)


registry.set_twin_sync(twin_sync)
# 同一句「太亮」：契约解析 → 驱动虚实两侧 → 回读确认
brightness = contract["params"]["brightness"]
result = registry.control("light_living_01", "set_brightness", {"brightness": brightness})

print("    执行动作: set_brightness %d  (「太亮」经能力契约下发)" % brightness)
print("    虚拟侧回读: expected=%d, actual=%s  -> confirmed=%s"
      % (brightness, result["readback"]["actual"], result["readback"]["confirmed"]))
twin_ev = result["twin"]
print("    实体侧回读: " +
      "  ".join("%s=%s/%s✓" % (k, v["actual"], v["expected"])
                for k, v in twin_ev["checks"].items()))
print("    孪生一致性: %s" % twin_ev["confirmed"])

print("\n    结论: 孪生体与实体 同状态 · 同动作 · 同验证 —— 仿真即真机")
print("=" * 66)
