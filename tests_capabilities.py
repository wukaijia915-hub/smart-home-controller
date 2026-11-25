# -*- coding: utf-8 -*-
"""能力注册表 + 事件路由 + 执行状态契约 模块测试（standalone，python tests_capabilities.py）。

覆盖：能力清单加载、别名归一、口语解析、设备类别契约、孪生配对、
事件路由匹配/冷却/fan-out、回读确认三态（confirmed/delivered/failed）。
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from backend.devices.registry import DeviceRegistry
from backend.capabilities import CapabilityRegistry
from backend.event_routes import EventRouter

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


class StubManager:
    """测试用 ConnectionManager：记录广播消息，不依赖事件循环。"""

    def __init__(self):
        self.messages = []

    async def broadcast(self, message: dict):
        self.messages.append(message)


print("== 能力注册表 ==")
caps = CapabilityRegistry()
check("能力清单加载", len(caps.capabilities) >= 15,
      f"实际 {len(caps.capabilities)}")
check("别名归一：热→climate", caps.canonical("好热") == "climate.set_temperature")
check("类别契约：set_temperature 支持 ac",
      caps.supports_category("climate.set_temperature", "ac"))
check("类别契约：set_temperature 不支持 light",
      not caps.supports_category("climate.set_temperature", "light"))

print("== 口语解析 ==")
r = caps.resolve("我好热啊")
check("「好热」→ 调温能力", r is not None and r["capability"] == "climate.set_temperature")
check("参数覆盖：好热=24℃", r is not None and r["params"]["temperature"] == 24)
r = caps.resolve("这屋太亮了")
check("「太亮」→ 亮度能力", r is not None and r["capability"] == "lighting.set_brightness")
check("参数覆盖：太亮=20%", r is not None and r["params"]["brightness"] == 20)
r = caps.resolve("拉上窗帘")
check("「拉上窗帘」→ 关窗帘", r is not None and r["command"] == "close")
check("未命中返回 None", caps.resolve("今天天气怎么样") is None)
r = caps.resolve("风大点")
check("「风大点」→ 风扇高档", r is not None and r["params"]["speed"] == "high")

print("== 能力→设备筛选 ==")
reg = DeviceRegistry()
ac_devices = caps.find_devices("climate.set_temperature", reg)
check("调温能力命中空调类", len(ac_devices) >= 2 and all(d.category == "ac" for d in ac_devices),
      f"实际 {[d.device_id for d in ac_devices]}")
check("离线设备被排除", len(ac_devices) < len(reg.all()))

print("== 执行状态契约 + 回读确认 ==")
reg2 = DeviceRegistry()
r = reg2.control("light_living_01", "set_brightness", {"brightness": 80})
check("调亮度→confirmed", r["ok"] and r["status"] == "confirmed", str(r))
check("回读证据：80 已确认", r["readback"]["confirmed"] and r["readback"]["actual"] == 80,
      str(r.get("readback")))
r = reg2.control("tv_living_01", "next_channel", {})
check("无可回读命令→delivered", r["ok"] and r["status"] == "delivered", str(r))
r = reg2.control("light_living_01", "不存在的命令", {})
check("不支持命令→failed", not r["ok"] and r["status"] == "failed")
r = reg2.control("不存在设备", "turn_on", {})
check("不存在设备→error", not r["ok"] and r["status"] == "error")
r = reg2.control("ac_living_01", "set_temperature", {"temperature": 24})
check("空调温度→confirmed 且回读 24", r["ok"] and r["readback"]["confirmed"]
      and r["readback"]["actual"] == 24, str(r.get("readback")))

print("== 孪生配对（可执行数字孪生） ==")
caps2 = CapabilityRegistry()
twin = caps2.pair_twin("light_living_01", "matter_light_01", name="Matter 实体灯")
reg3 = DeviceRegistry()

def twin_sync(device, attrs):
    t = caps2.get_twin(device.device_id)
    if t is None:
        return None
    t.apply(attrs)
    return t.readback(attrs)

reg3.set_twin_sync(twin_sync)
r = reg3.control("light_living_01", "set_brightness", {"brightness": 60})
check("孪生体执行后实体侧回读确认", r.get("twin", {}).get("confirmed") is True, str(r.get("twin")))
check("实体侧状态与虚拟预期一致",
      r.get("twin", {}).get("checks", {}).get("brightness", {}).get("actual") == 60,
      str(r.get("twin")))

print("== 事件路由 ==")
manager = StubManager()
reg4 = DeviceRegistry()
router = EventRouter(reg4, manager)
event = {"type": "fall_detected", "source": "edge_vision",
         "payload": {"severity": "critical", "summary": "检测到有人跌倒"}}
ev = router.route(event)
check("跌倒事件命中 fall_alert", "fall_alert" in ev.matched_routes, str(ev.matched_routes))
device_actions = [a for a in ev.actions if a.get("type") == "device"]
alert_actions = [a for a in ev.actions if a.get("type") == "alert"]
check("路由含设备动作", len(device_actions) >= 1)
check("设备动作带 confirmed 证据", device_actions[0].get("status") == "confirmed",
      str(device_actions[0].get("readback")))
check("路由含告警广播", len(alert_actions) == 1 and len(manager.messages) == 1)
check("告警消息含事件语义", "跌倒" in alert_actions[0]["message"])

print("== 事件路由：fan-out + 冷却 ==")
ev2 = router.route({"type": "no_person", "source": "edge_vision",
                    "payload": {"summary": "未检测到人员"}})
dev2 = [a for a in ev2.actions if a.get("type") == "device"]
check("离家事件并行执行 3 个设备动作", len(dev2) == 3, str([a.get("device_id") for a in dev2]))
check("3 个动作全部 confirmed", all(a.get("status") == "confirmed" for a in dev2))
# 冷却：60s 内重复跌倒事件应跳过
ev3 = router.route({"type": "fall_detected", "source": "edge_vision",
                    "payload": {"severity": "critical"}})
check("冷却期重复跌倒被跳过",
      any(a.get("type") == "cooldown" and a.get("status") == "skipped" for a in ev3.actions),
      str([a.get("type") for a in ev3.actions]))
# 不匹配事件：无路由命中。
# 断言"被忽略的路由数 == 配置里的路由总数"，而不是写死 3 —— 原来写死数字，
# 结果阶段 4 往 event_routes.json 加了 7 条环境联动路由，这条测试就假失败了。
# 被测的性质是"没有任何路由命中"，它不该随配置文件里路由条数变化。
ev4 = router.route({"type": "person_present", "source": "edge_vision", "payload": {}})
_total_routes = len(router.raw.get("routes", []))
check("未配置的事件不触发路由",
      len(ev4.matched_routes) == 0 and len(ev4.ignored_routes) == _total_routes,
      f"matched={ev4.matched_routes} ignored={len(ev4.ignored_routes)}/{_total_routes}")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
