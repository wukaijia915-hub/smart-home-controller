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

# ---------------------------------------------------------------- 6. 架构不变量
print("\n== 6. 架构不变量：LLM 只提议，护栏负责拒绝 ==")

# Manager 是纯护栏，不应持有 LLM
check("Manager 不持有 LLM", not hasattr(agent.manager, "llm"),
      f"发现属性 {[a for a in dir(agent.manager) if 'llm' in a.lower()]}")

# 慢环：Root 单次提议 → Manager 零调用。用假 LLM 使测试离线可复现
_original_chat = agent.llm.chat_json
_original_available = agent.llm.available
_fake = {"calls": 0}

def _fake_chat_json(messages, **kwargs):
    _fake["calls"] += 1
    return {
        "is_command": True,
        "reply": "好的，已为您把灯光调柔和。",
        "actions": [{"device_id": "light_living_01", "command": "set_brightness",
                     "params": {"brightness": 30}}],
    }

agent.llm.chat_json = _fake_chat_json
agent.llm.available = True
try:
    res = agent.decide("把灯光调得柔和一些")
finally:
    agent.llm.chat_json = _original_chat
    agent.llm.available = _original_available

check("慢环全程只调用一次 LLM", _fake["calls"] == 1, f"实际 {_fake['calls']} 次")
check("模型提议的动作被 Manager 执行",
      any(a.get("device_id") == "light_living_01" and a.get("command") == "set_brightness"
          for a in res.get("actions", [])),
      str(res.get("actions")))

# 注意：测试输入的选取很关键 —— 必须能被漏斗①~⑤ 放行才走得到慢环。
# 例如「打开客厅的灯」会被设备正则命中，根本到不了 LLM，用它测"模型提议被拒绝"
# 会得到假通过（实际是规则层干的）。下面两个输入均已确认会走慢环，
# 并由 `_fake["calls"] == 1` 显式断言这一点。
SLOW_PATH_INPUT_BAD_DEVICE = "把整个屋子的氛围弄温馨一点"
SLOW_PATH_INPUT_HIGH_RISK = "把家里的门锁处理一下"

# 模型提议了不存在的设备 → Manager 必须拒绝而非执行
_fake2 = {"calls": 0}
def _fake_bad_action(messages, **kwargs):
    _fake2["calls"] += 1
    return {"is_command": True, "reply": "好的",
            "actions": [{"device_id": "light_atlantis_99", "command": "turn_on", "params": {}}]}

agent.llm.chat_json = _fake_bad_action
agent.llm.available = True
try:
    res2 = agent.decide(SLOW_PATH_INPUT_BAD_DEVICE)
finally:
    agent.llm.chat_json = _original_chat
    agent.llm.available = _original_available

check("（前提）该输入确实走了慢环", _fake2["calls"] == 1, f"实际 {_fake2['calls']} 次 LLM 调用")
check("模型提议不存在的设备 → 被拒绝而非执行",
      not res2.get("actions")
      and any(not r.get("ok") for r in res2.get("results", [])),
      f"actions={res2.get('actions')} results={res2.get('results')}")
check("拒绝原因被记入决策链路",
      any(not t.get("ok") for t in res2.get("decision_trace", [])),
      str([(t.get("step"), t.get("ok")) for t in res2.get("decision_trace", [])]))

# 模型提议高危动作 → 仍须挂起二次确认（护栏不因来源是模型而放行）
_fake3 = {"calls": 0}
def _fake_high_risk(messages, **kwargs):
    _fake3["calls"] += 1
    return {"is_command": True, "reply": "好的",
            "actions": [{"device_id": "lock_entry_01", "command": "unlock", "params": {}}]}

agent.llm.chat_json = _fake_high_risk
agent.llm.available = True
try:
    res3 = agent.decide(SLOW_PATH_INPUT_HIGH_RISK)
finally:
    agent.llm.chat_json = _original_chat
    agent.llm.available = _original_available

check("（前提）该输入确实走了慢环", _fake3["calls"] == 1, f"实际 {_fake3['calls']} 次 LLM 调用")
check("模型提议高危动作 → 挂起待确认",
      res3.get("requires_confirmation") is True and bool(res3.get("pending_id")),
      f"requires_confirmation={res3.get('requires_confirmation')}")

# ---------------------------------------------------------------- 7. 工具层护栏
print("\n== 7. 工具层不得绕过护栏（/api/tools/call 与 MCP tools/call）==")

from backend.toolkit import tool_registry as _tr
from backend.toolkit.guard import ManagerGuard

_tool_reg = DeviceRegistry()
_tool_caps = CapabilityRegistry()
_tool_caps.auto_pair(_tool_reg.all())
_tool_agent = HomeAgent(_tool_reg, capabilities=_tool_caps)
_tr.build_from_devices(_tool_reg)

# 未装配护栏时必须 fail closed（曾经的真实漏洞：直连 control 可解锁门锁）
_r = _tr.call("lock_entry_01__unlock", {})
check("未装配护栏 → 拒绝执行设备动作",
      _r.get("ok") is False and "护栏" in str(_r.get("error", "")),
      str(_r.get("error"))[:70])
check("未装配护栏时设备状态未被改变",
      _tool_reg.get("lock_entry_01").get("locked") is True)

# 装配护栏后：高危必须挂起且不执行
_tr.bind_actuation_guard(ManagerGuard(_tool_agent.manager))

_r = _tr.call("lock_entry_01__unlock", {})
check("工具直调 unlock → 挂起待确认（不执行）",
      _r.get("ok") is False and _r.get("requires_confirmation") is True
      and bool(_r.get("pending_id")),
      str({k: _r.get(k) for k in ("ok", "requires_confirmation", "pending_id")}))
check("挂起期间门锁仍为锁定", _tool_reg.get("lock_entry_01").get("locked") is True)

_r = _tr.call("gas_stove_kitchen_01__turn_on", {})
check("工具直调燃气灶点火 → 挂起待确认（不执行）",
      _r.get("ok") is False and _r.get("requires_confirmation") is True)
check("挂起期间燃气灶未点火", _tool_reg.get("gas_stove_kitchen_01").get("on") is False)

# 普通动作正常执行，且带回读证据
_r = _tr.call("light_living_01__set_brightness", {"brightness": 45})
check("工具直调普通动作 → 正常执行且回读 confirmed",
      _r.get("ok") is True and _r.get("result", {}).get("status") == "confirmed",
      str(_r.get("result", {}).get("status")))
check("设备状态确实被改变",
      _tool_reg.get("light_living_01").get("brightness") == 45)

# 参数校验仍然生效
_r = _tr.call("ac_living_01__set_temperature", {"temperature": 99})
check("工具直调参数越界 → 被拒", _r.get("ok") is False, str(_r.get("error"))[:60])

# 确认后应能真正执行（证明挂起不是死路）
_pid = _tr.call("lock_entry_01__unlock", {}).get("pending_id")
_conf = _tool_agent.confirm(_pid)
check("确认后高危动作可执行（挂起不是死路）",
      _conf.get("ok") is True and _tool_reg.get("lock_entry_01").get("locked") is False,
      str(_conf.get("error", _conf.get("status"))))

# ---------------------------------------------------------------- 8. 按意图裁剪
print("\n== 8. 工具/设备清单按意图裁剪（工具越多，选择精度越低）==")
from backend.toolkit.scoping import select_devices, select_tools, detect_signal

_all_devices = registry.all()
_picked, _meta = select_devices("卧室太亮", _all_devices)
check("卧室信号：只留卧室设备", len(_picked) == 4 and {d.room for d in _picked} == {"卧室"},
      f"{len(_picked)} 台")

_picked2, _meta2 = select_devices("把屋子氛围弄温馨一点", _all_devices)
check("★ 无信号不裁剪：模糊指令保留全量视野",
      len(_picked2) == len(_all_devices) and not _meta2["narrowed"],
      f"{len(_picked2)} 台 narrowed={_meta2['narrowed']}")

_picked3, _ = select_devices("打开客厅吊灯", _all_devices)
check("设备名精确命中：只留客厅吊灯",
      [d.device_id for d in _picked3] == ["light_living_01"],
      str([d.device_id for d in _picked3]))

_tools_all = _tr.list_tools()
_dev_map = {d.device_id: d for d in _all_devices}
_scoped, _tmeta = select_tools("门锁", _tools_all, _dev_map)
check("★ 高危命令默认不可见（门锁回合不出现 unlock）",
      not any(t.command == "unlock" for t in _scoped) and _tmeta["high_risk_hidden"] >= 1,
      f"kept={[t.name for t in _scoped]} hidden={_tmeta['high_risk_hidden']}")

_scoped_hr, _ = select_tools("解锁门锁", _tools_all, _dev_map)
check("显式提到「解锁」才放出高危命令",
      any(t.command == "unlock" for t in _scoped_hr),
      str([t.name for t in _scoped_hr]))

_scoped_no, _tmeta_no = select_tools("把屋子氛围弄温馨一点", _tools_all, _dev_map)
check("无信号时只隐藏高危命令，不裁设备",
      len(_scoped_no) == len(_tools_all) - _tmeta_no["high_risk_hidden"]
      and _tmeta_no["high_risk_hidden"] == 4,
      f"{len(_scoped_no)}/{len(_tools_all)} hidden={_tmeta_no['high_risk_hidden']}")

_bedroom_tools, _ = _tr.list_tools_scoped("卧室")
check("工具注册表按意图裁剪：卧室 → 16 个工具（4 台设备）",
      len(_bedroom_tools) == 16, f"{len(_bedroom_tools)} 个")

_scope_meta = {}
_prompt = root._build_system_prompt("卧室太亮", _scope_meta)
check("慢环 prompt 只含裁剪后的设备行 + 显式告知模型",
      _prompt.count("\n- ") == 4 and "本轮只列出" in _prompt,
      f"{_prompt.count('- ')} 行")
check("裁剪结果写回 scope（供 trace / 审计复核）", _scope_meta.get("narrowed") is True, str(_scope_meta))

_prompt_all = root._build_system_prompt("把屋子氛围弄温馨一点", {})
check("模糊指令的 prompt 仍是全量设备",
      _prompt_all.count("\n- ") == len(_all_devices), f"{_prompt_all.count('- ')} 行")

# ---------------------------------------------------------------- 汇总
print()
print("=" * 60)
total = PASSED + FAILED
print(f"  结果: {PASSED} passed, {FAILED} failed  (共 {total} 项)")
print("=" * 60)

sys.exit(1 if FAILED else 0)
