# -*- coding: utf-8 -*-
"""五级意图漏斗测试：优先级、技能触发词约束、能力契约语义、护栏不被绕过。

漏斗设计（从最具体到最宽泛，这个顺序本身就是被测对象）：
    ① 空输入保护 → ② 场景规则 → ③ 技能触发词 → ④ 能力契约别名 → ⑤ 设备指令正则 → ⑥ LLM 慢环

为什么需要这组测试：
- 漏斗顺序错了会静默出错。实测过的例子：设备正则 `窗帘.*(开|拉开)` 排在能力契约
  之前，会把「窗帘开一半」抢成全开（能力契约给的是 position=50）。
- 技能触发词若与场景关键词重叠，会被上游漏斗截胡成为永不命中的死条目。
- 能力契约必须逐条能被真实设备满足，否则配了也是空转。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.agent import HomeAgent
from backend.capabilities import CapabilityRegistry
from backend.devices.registry import DeviceRegistry
from backend.scenes import SCENES
from backend.skills import BUILTIN_SKILLS

PASSED, FAILED = 0, 0


def check(label: str, condition: bool, detail: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {label}")
    else:
        FAILED += 1
        print(f"  ✗ {label}  {detail}")


registry = DeviceRegistry()
caps = CapabilityRegistry()
caps.auto_pair(registry.all())
agent = HomeAgent(registry, capabilities=caps)
root = agent.root


def plan(text: str):
    return root.plan(text, {}, [])


print("=" * 60)
print("  灵犀 — 五级意图漏斗测试")
print("=" * 60)

# ---------------------------------------------------------------- 1. 漏斗优先级
print("\n== 1. 漏斗优先级（从最具体到最宽泛）==")

# 场景规则必须压过其余全部漏斗：它是标准场景集（S1-S11）的评分依据
for text, expect_scene in [("我回家了", "home"), ("看电影", "movie"), ("好热", "cool")]:
    p = plan(text)
    hit = root._match_scene(text) == expect_scene
    check(f"场景优先：「{text}」→ {expect_scene}", hit and p.source == "rule" and p.sub_tasks)

# 能力契约必须压过宽泛的设备正则（回归：曾把「窗帘开一半」抢成全开）
p = plan("窗帘开一半")
check("能力契约压过宽泛正则：窗帘开一半 → position=50",
      len(p.sub_tasks) == 1
      and p.sub_tasks[0].command == "set_position"
      and p.sub_tasks[0].params.get("position") == 50,
      str([(s.command, s.params) for s in p.sub_tasks]))

# 设备正则仍是兜底：带具体数值的指令由它处理（能力契约里没有数字别名）
p = plan("空调调到24度")
check("设备正则兜底：空调调到24度",
      any(s.command == "set_temperature" and s.params.get("temperature") == 24
          for s in p.sub_tasks),
      str([(s.command, s.params) for s in p.sub_tasks]))

# 技能触发词命中漏斗③
p = plan("阅读模式")
check("技能漏斗：「阅读模式」展开为动作序列",
      len(p.sub_tasks) >= 4 and p.source == "rule",
      f"{len(p.sub_tasks)} 个子任务")

# ---------------------------------------------------------------- 2. 技能触发词约束
print("\n== 2. 技能触发词不得与场景关键词重叠（否则是死条目）==")

scene_keywords = ["回家", "到家", "回来了", "出门", "离家", "去上班", "离开家", "我走了",
                  "看电影", "观影", "开电视", "看个片", "睡觉", "睡眠", "就寝", "上床",
                  "晚安", "睡了", "会客", "客人", "朋友来", "接待", "起床", "早上好",
                  "早安", "好热", "太热", "热死", "降温", "凉快", "好冷", "太冷", "冻",
                  "升温", "暖和", "省电", "节能", "环保"]

collisions = []
for skill in BUILTIN_SKILLS.values():
    for kw in skill.get("trigger_keywords", []):
        for scene_kw in scene_keywords:
            if scene_kw in kw:
                collisions.append(f"{skill['id']}.{kw} ⊃ {scene_kw}")
check("内置技能触发词与场景关键词无重叠", not collisions, str(collisions))

triggered = [s["id"] for s in BUILTIN_SKILLS.values() if s.get("trigger_keywords")]
check("至少有一个内置技能带触发词（漏斗③ 有可测路径）", bool(triggered), str(triggered))

# 每个带触发词的技能都必须真能命中
for skill in BUILTIN_SKILLS.values():
    kws = skill.get("trigger_keywords") or []
    if not kws:
        continue
    matched = agent.root.skills.match_by_keyword(kws[0])
    check(f"技能「{skill['name']}」触发词可命中", matched is not None
          and matched.get("id") == skill["id"])

# ---------------------------------------------------------------- 3. 能力契约
print("\n== 3. 能力契约：别名语义与设备可达性 ==")

# 参数语义：口语程度词必须映射到正确的方向，别出现"太亮"反而更亮
semantics = {
    "太亮": ("set_brightness", "brightness", 20),
    "太暗": ("set_brightness", "brightness", 90),
    "调暗": ("set_brightness", "brightness", 20),
    "调亮": ("set_brightness", "brightness", 90),
    "暖光": ("set_color_temp", "color_temp", 3000),
    "静音": ("set_volume", "volume", 0),
    "大声点": ("set_volume", "volume", 60),
    "风大点": ("set_speed", "speed", "high"),
    "风小点": ("set_speed", "speed", "low"),
    "太干": ("set_target", "target", 65),
    "制冷": ("set_mode", "mode", "cool"),
    "制热": ("set_mode", "mode", "heat"),
}
for text, (cmd, key, value) in semantics.items():
    p = plan(text)
    ok = (p.sub_tasks and p.sub_tasks[0].command == cmd
          and p.sub_tasks[0].params.get(key) == value)
    check(f"别名语义：「{text}」→ {cmd}.{key}={value}", ok,
          str([(s.command, s.params) for s in p.sub_tasks]))

# 每条能力契约都必须能找到至少一台真实设备（否则配了也空转）
unreachable = []
for name, cap in caps.capabilities.items():
    if not cap.command:
        continue        # 声明式能力（如 safety.alert 只做播报，不落设备）
    if not caps.find_devices(name, registry):
        unreachable.append(name)
check("每条能力契约都能找到实现设备", not unreachable, str(unreachable))

# 每个别名都不该被更上游的漏斗抢走（除非它本来就该被场景接管的那些）
shadowed = []
for kw in caps.alias_map:
    if root._match_scene(kw):
        continue        # "好热"等本来就该走场景，动作集更丰富
    p = plan(kw)
    if not p.sub_tasks and p.source != "rule":
        shadowed.append(kw)
check("能力别名均可达（场景已接管的不计）", not shadowed, str(shadowed))

# 房间偏好：提到卧室不应作用到客厅设备
p = plan("卧室太亮")
rooms = {registry.get(s.device_id).room for s in p.sub_tasks if registry.get(s.device_id)}
check("房间偏好：卧室太亮只作用于卧室", rooms == {"卧室"}, str(rooms))

# ---------------------------------------------------------------- 4. 护栏不被绕过
print("\n== 4. 护栏：漏斗接线不得绕过高危二次确认 ==")

high_risk_inputs = {
    "解锁": "门锁解锁",
    "打开燃气灶": "打开燃气灶",
    "打开摄像头": "开启摄像头直播",
}
for text, desc in high_risk_inputs.items():
    res = agent.decide(text)
    check(f"高危仍挂起：「{text}」", res["requires_confirmation"] is True
          and res.get("pending_id"),
          f"requires_confirmation={res['requires_confirmation']}")

# 上锁不是高危（HIGH_RISK 只含 unlock/set_lock_mode），应直接执行
res = agent.decide("锁门")
check("上锁不属于高危，直接执行", res["requires_confirmation"] is False
      and any(a.get("command") == "lock" for a in res["actions"]))

# 技能内的高危命令也必须被拦（不能借技能批量绕过）
from backend.lease import LeaseValidator
from backend.skills import SkillManager
sm = SkillManager()
sm.create({
    "id": "_test_risky_skill", "name": "测试用高危技能",
    "actions": [{"device_id": "lock_entry_01", "command": "unlock", "params": {}}],
    "trigger_keywords": [], "built_in": False,
})
r = sm.execute("_test_risky_skill", registry)
check("技能内的高危命令被拒绝执行",
      r["succeeded"] == 0 and all(not a.get("ok") for a in r["actions"]),
      str(r["actions"]))
sm.delete("_test_risky_skill")

# ---------------------------------------------------------------- 5. 无 LLM 依赖
print("\n== 5. 规则层不依赖 LLM ==")

# 漏斗①②③④⑤ 全部在 plan() 内同步完成，物理上不可能触达 LLM
llm_calls = {"n": 0}
original = agent.llm.chat_json
agent.llm.chat_json = lambda *a, **k: (llm_calls.__setitem__("n", llm_calls["n"] + 1), None)[1]
for text in ["我回家了", "看电影", "阅读模式", "太亮", "打开客厅灯", "扫地", "锁门"]:
    plan(text)
agent.llm.chat_json = original
check("规则命中的输入不触发 LLM 调用", llm_calls["n"] == 0, f"实际调用 {llm_calls['n']} 次")

# ---------------------------------------------------------------- 汇总
print()
print("=" * 60)
total = PASSED + FAILED
print(f"  结果: {PASSED} passed, {FAILED} failed  (共 {total} 项)")
print("=" * 60)

sys.exit(1 if FAILED else 0)
