# -*- coding: utf-8 -*-
"""记忆三源测试：手册检索 / 习惯统计与偏好一致性 / 经验复用的确定性。

为什么要这组测试：

1. **确定性是经验复用的前提**。心镜用 `random.sample` 抽样，导致同一请求每次注入
   不同案例 —— "自进化有没有用"这个问题在那种实现下**无法被验证**。本项目改成
   确定性排序，正是为了让下面这组断言成立：同样的输入必须注入同样的案例。
2. **偏好冲突必须落到具体设备上**。实测踩到过：跨设备取全局众数时，客厅空调
   长期 24℃、卧室空调长期 26℃ 会被抹平成"看起来一致"，冲突检测静默失效。
3. **记忆层不得有执行能力**。它只能影响 prompt，不能碰设备 —— 这条靠代码评审
   守不住（半年后有人加一句"自动应用偏好"谁也不会注意），所以做成静态扫描。
"""
import os
import re
import sys
import tempfile

# ⚠️ 必须在导入 backend 之前设置：数据目录在模块导入期就被解析，
# 晚一步设置就会把测试写进真实用户数据里（这正是此前积累出
# "用户最常用场景 home（1842 次）"这种测试噪声的原因）。
_TMP_DIR = tempfile.mkdtemp(prefix="lx_mem_")
os.environ["LINGXI_DATA_DIR"] = _TMP_DIR
os.environ["LLM_OFFLINE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ast
import itertools
from pathlib import Path

from backend.memory import experience as exp_mod
from backend.memory import habits as habits_mod
from backend.memory import manuals as manuals_mod
from backend.memory.experience import ExperienceStore, derive_rating
from backend.memory.habits import HabitStore
from backend.memory.manuals import keyword_score

PASS, FAIL = 0, 0
_seq = itertools.count()


def check(label: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}  {detail}")


def fresh_habits() -> HabitStore:
    """每次给一个**独立文件**。

    默认构造的多个 HabitStore 共用同一个路径，于是第二个实例会读到第一个实例
    写下的数据 —— 这让"样本不足"这类断言变得不可复现（实测踩到过：
    上一条用例写入的 8 条观察把本条用例的 2 条样本淹没，"样本不足"用例假失败）。
    """
    return HabitStore(path=Path(_TMP_DIR) / f"habits_{next(_seq)}.json")


def fresh_experience() -> ExperienceStore:
    return ExperienceStore(path=Path(_TMP_DIR) / f"exp_{next(_seq)}.jsonl")


def code_only(path: Path) -> str:
    """去掉文档字符串与注释后的源码。

    静态扫描必须扫**代码结构**，不能扫散文：本模块的 docstring 里正大光明地写着
    "不导入 DeviceRegistry"和"学生姓名 student_name 这种字段不该出现"，
    把说明当成违规会让这条测试变成"谁解释谁就违规"。
    """
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"#[^\n]*", "", src)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return src
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            ds = ast.get_docstring(node, clean=False)
            if ds:
                src = src.replace(ds, "")
    # 模块级 docstring 也走同一处理（上一步已覆盖 ast.Module）
    return src


print("=" * 60)
print("  灵犀 — 记忆层三源测试")
print("=" * 60)
print(f"  数据目录: {os.environ['LINGXI_DATA_DIR']}")

# ---------------------------------------------------------------- 1. 手册
print("\n== 1. 手册检索 ==")
manuals = manuals_mod.ManualStore()
check("手册已加载（>0 条）", len(manuals.manuals) > 0, str(len(manuals.manuals)))
hit = manuals.search("空调好像很费电", top_k=2)
check("关键词命中空调手册", any("空调" in m.get("name", "") for m in hit),
      str([m.get("name") for m in hit]))
check("命中结果带 _score（可解释为何命中）",
      all("_score" in m and m["_score"] > 0 for m in hit))
check("无关查询返回空", manuals.search("zxqwv", top_k=2) == [])
check("空查询返回空", manuals.search("") == [])
check("get_context 无命中时返回空串（不编造）", manuals.get_context("zxqwv") == "")

# 打分函数是共用件（手册 + 经验都用它），因此单独钉住它的确定性
check("keyword_score 确定性", keyword_score("客厅空调很费电", "空调费电")
      == keyword_score("客厅空调很费电", "空调费电"))
check("keyword_score 相关 > 不相关",
      keyword_score("客厅空调的节能提示", "空调") > keyword_score("窗帘开合度", "空调"))

# ---------------------------------------------------------------- 2. 习惯
print("\n== 2. 习惯统计 + 偏好一致性校验 ==")
h = fresh_habits()
for _ in range(4):
    h.record_command("ac_living_01", "set_temperature", {"temperature": 24})
for _ in range(5):
    h.record_command("ac_bedroom_01", "set_temperature", {"temperature": 26})
h.record_scene("home")
h.record_scene("home")
h.record_scene("home")

check("命令计数已记录", h.data["commands"].get("ac_living_01:set_temperature") == 4,
      str(h.data["commands"]))
check("场景计数已记录", h.data["scenes"].get("home") == 3)
check("参数偏好样本已记录",
      len(h.data["preferences"]["ac_living_01:set_temperature:params"]) == 4)
check("空参数不写入偏好样本（避免噪声样本）",
      h.record_command("x", "y", {}) is None and "x:y:params" not in h.data["preferences"])

# 显式偏好抽取：只在用户明确表达偏好时记录
check("抽取「我喜欢26度」", (h.record_declared("我喜欢26度") or {}).get("value") == 26.0)
check("抽取「以后都 24 度」（含空格）",
      (h.record_declared("以后都 24 度") or {}).get("value") == 24.0)
check("抽取「% 单位」偏好亮度",
      (h.record_declared("我习惯亮度 80%") or {}).get("preference") == "brightness")
check("普通控制指令不产生偏好声明",
      h.record_declared("空调调到24度") is None,
      "「调到24度」是这一次的指令，不是长期偏好")
check("寒暄不产生偏好声明", h.record_declared("你好呀") is None)

# 一致性校验：声明 26，观察侧客厅 24 / 卧室 26 —— 只应报客厅
h2 = fresh_habits()
for _ in range(4):
    h2.record_command("ac_living_01", "set_temperature", {"temperature": 24})
for _ in range(5):
    h2.record_command("ac_bedroom_01", "set_temperature", {"temperature": 26})
h2.record_declared("以后都26度")
conflicts = h2.consistency_check(name_of=lambda d: {"ac_living_01": "客厅空调"}.get(d, d))
check("冲突检测命中客厅空调", len(conflicts) == 1 and conflicts[0]["device_id"] == "ac_living_01",
      str(conflicts))
check("卧室空调（与声明一致）不报冲突",
      all(c["device_id"] != "ac_bedroom_01" for c in conflicts),
      "跨设备取全局众数会把两台设备各自的稳定偏好抹平，这条断言就是防它")
check("冲突文案是问句而非动作",
      bool(conflicts) and conflicts[0]["question"].endswith("吗？"))
check("冲突文案用了注入的设备显示名",
      bool(conflicts) and "客厅空调" in conflicts[0]["question"])
check("冲突带可复核的样本数", bool(conflicts) and conflicts[0]["observations"] >= 3)

# 样本不足不下结论
h3 = fresh_habits()
h3.record_declared("我很喜欢26度")
h3.record_command("ac_living_01", "set_temperature", {"temperature": 18})
h3.record_command("ac_living_01", "set_temperature", {"temperature": 18})
check("观察样本 <3 次不报冲突（不拿噪声下结论）", h3.consistency_check() == [],
      str(h3.consistency_check()))

# 统计与提示
check("stats 含观察到的偏好（按设备）",
      "ac_living_01" in h2.stats()["observed_preferences"]["temperature"])
check("hint 明确区分「声明」的偏好", "明确表示偏好" in h2.hint())
check("hint 数据不足时不编造", fresh_habits().hint() == "")

# 落盘策略：**延迟写**（记录只置脏，由内环统一 flush）
#
# 这不是"顺手改的优化"，而是一个实测出来的真问题：`_save()` 每次都要序列化整个
# JSON + 写盘 + 原子替换，而它被 `HomeAgent.decide()` 对**每一条成功执行的动作**
# 调用 —— 一次「我回家了」（4 条动作）就是 4 次全文件重写。
# 实测：完整 decide() 8.74ms，屏蔽习惯落盘后 0.13ms，即**98% 的规则层延迟是磁盘 I/O**。
# 习惯统计不是事务数据，崩溃时丢几秒计数可以接受，每个指令等 8ms 磁盘不行。
h2.record_command("tv_living_01", "set_volume", {"volume": 20})
h2.record_scene("movie")
check("★ 记录后不立即写盘（延迟写，避免每条动作重写整个文件）",
      h2._dirty is True, "记录后应处于脏状态")
check("未 flush 时文件里还没有这些记录",
      (not h2.path.exists())
      or ("tv_living_01:set_volume" not in h2.path.read_text(encoding="utf-8")),
      "延迟写生效（连文件都可能还没被创建）")
check("flush 返回 True 表示确实写了", h2.flush() is True)
check("flush 后无脏标记", h2._dirty is False)
check("flush 幂等：无改动时返回 False 不再写盘", h2.flush() is False)
_reloaded = HabitStore(path=h2.path)
check("落盘内容可被重新读出（换一个实例读同一个文件）",
      _reloaded.data["commands"].get("tv_living_01:set_volume") == 1,
      "延迟写 + 原子替换之后，数据必须能被完整读回")
check("延迟写的全部记录都在（命令与场景都不丢）",
      _reloaded.data["scenes"].get("movie") == 1, str(_reloaded.data["scenes"]))
tmp_leftovers = list(Path(h2.path.parent).glob("*.tmp"))
check("原子落盘无 .tmp 残留", not tmp_leftovers, str(tmp_leftovers))

# 迁移只在规范目录发生（否则测试会导入生产数据，断言不可复现）
check("临时数据目录不触发旧数据迁移",
      fresh_habits().data["commands"] == {},
      f"期望空库，实际 {fresh_habits().data['commands']}")

# ---------------------------------------------------------------- 3. 经验
print("\n== 3. 经验复用（确定性排序） ==")
check("评分档位：全成功=5", derive_rating(3, 3) == 5)
check("评分档位：部分成功=3", derive_rating(1, 3) == 3)
check("评分档位：全失败=1", derive_rating(0, 3) == 1)
check("评分档位：无动作=0（不可评分）", derive_rating(0, 0) == 0)

e = fresh_experience()
ids = []
for i, (req, rating) in enumerate([
        ("客厅有点闷", 5), ("客厅想看点片子", 5), ("卧室有点闷", 3),
        ("厨房灯坏了", 1), ("客厅空调有点吵", 5)]):
    ids.append(e.record("fuzzy_command", f"客厅|ac{i}", req,
                        [{"device_id": "ac_living_01", "command": "turn_on"}],
                        "已处理", rating=rating, rating_source="auto_execution"))
check("经验写入返回 id", all(ids) and len(set(ids)) == 5, str(ids))

first = e.retrieve(query="客厅有点闷", limit=2)
second = e.retrieve(query="客厅有点闷", limit=2)
check("检索默认过滤低分（>=4）",
      all(x["rating"] >= 4 for x in first), str([x["rating"] for x in first]))
check("★ 确定性：同样输入两次检索结果完全相同（无 random.sample）",
      [x["id"] for x in first] == [x["id"] for x in second],
      f"{[x['id'] for x in first]} vs {[x['id'] for x in second]}")
check("相关度生效：查询「客厅有点闷」命中该案例",
      first and first[0]["request"] == "客厅有点闷", str([x["request"] for x in first]))

ctx = e.build_context(prefer_category="fuzzy_command", query="客厅有点闷")
check("注入上下文含案例", "历史成功案例" in ctx and "客厅有点闷" in ctx, ctx[:80])
check("注入上下文不含低分案例", "厨房灯坏了" not in ctx)
check("last_injected 记录本轮注入（可复核）",
      len(e.last_injected) == len(first) and e.last_injected[0]["id"] == first[0]["id"])

check("无案例时返回空串（不编造经验）",
      fresh_experience().build_context(query="随便什么") == "")

# 用户显式评分覆盖自动评分
check("用户评分写入成功", e.rate(ids[0], 2, "其实不好用"))
after = e.retrieve(query="客厅有点闷", limit=5, min_rating=0)
rated = [x for x in after if x["id"] == ids[0]]
check("评分覆盖后 rating_source=user",
      rated and rated[0]["rating"] == 2 and rated[0]["rating_source"] == "user",
      str(rated))
check("低分案例被过滤出默认检索",
      ids[0] not in [x["id"] for x in e.retrieve(query="客厅有点闷", limit=5)])
check("非法评分被拒绝", not e.rate(ids[0], 9) and not e.rate("", 3))

st = e.stats()
check("stats 区分自动评分与用户评分",
      st["user_rated"] == 1 and st["note"].find("执行结果") >= 0, str(st["user_rated"]))
check("stats 样本不足时趋势如实说「数据不足」", st["rating_trend"] == "数据不足")
check("stats 只声明有写入方的 category",
      set(st["by_category"]) <= set(exp_mod.CATEGORIES), str(set(st["by_category"])))
check("非法 category 被归一而非丢弃",
      e.record("不存在的类", "k", "r", [], "x", rating=5) is not None)

# ---------------------------------------------------------------- 4. 静态不变量
print("\n== 4. 架构不变量（静态扫描） ==")
# 记忆层只能影响 prompt，不能影响世界。这条承诺靠评审守不住，所以做成会失败的测试。
mem_dir = Path(__file__).resolve().parent / "backend" / "memory"
mem_files = sorted(mem_dir.glob("*.py"))
check("扫描目标存在（否则下面的检查会静默通过）", len(mem_files) >= 3,
      str([p.name for p in mem_files]))

forbidden = ["registry.control", "Lease.issue", "ActuationGuard",
             "DeviceRegistry", "LeaseValidator", "import lease"]
violations = []
for py in mem_files:
    src = code_only(py)
    for token in forbidden:
        if token in src:
            violations.append(f"{py.name}: {token}")
check("记忆层无设备执行能力（不导入 registry/lease，不调用 control）",
      not violations, "; ".join(violations))

# 心镜用 random.sample 抽案例，那是"自进化无法被验证"的根因；本项目必须没有它
random_violations = [py.name for py in mem_files if "import random" in code_only(py)]
check("记忆层不使用 random（经验注入必须确定性）",
      not random_violations, "; ".join(random_violations))

field_violations = []
for py in mem_files:
    src = code_only(py)
    for token in ["student_name", "daily_report", "intervention_plan", "parent_letter"]:
        if token in src:
            field_violations.append(f"{py.name}: {token}")
check("字段已去领域化（无心镜的 student_name 等字段）",
      not field_violations, "; ".join(field_violations))

# 反向自检：扫描器本身必须是有效的（否则"零违规"可能只是因为它什么都没扫到）
_probe = Path(_TMP_DIR) / "probe.py"
_probe.write_text('"""不导入 DeviceRegistry 的说明。"""\n'
                  '# registry.control 也只在注释里\n'
                  'from ..devices import DeviceRegistry\n'
                  'x = DeviceRegistry()\n', encoding="utf-8")
check("扫描器自检：能抓到真违规、放过说明性文字",
      "DeviceRegistry" in code_only(_probe), code_only(_probe))

# ---------------------------------------------------------------- 5. 经验复用闭环
# 这是"成功案例回灌 prompt"这个说法的**唯一**证据来源。整条闭环有三段：
#   慢环规划 → 执行 → 写回经验库 → 下次同类请求注入 prompt
# 缺任何一段，这句话就只是口号。规则命中的路径不写经验（它下回还是走规则，
# 用不上），所以必须用**确认会走慢环**的输入。
print("\n== 5. 闭环：慢环规划 → 执行 → 写回 → 下次注入 ==")
from backend.agent import HomeAgent
from backend.capabilities import CapabilityRegistry
from backend.devices.registry import DeviceRegistry
from backend.memory.experience import experience as global_exp

registry = DeviceRegistry()
caps = CapabilityRegistry()
caps.auto_pair(registry.all())
agent = HomeAgent(registry, capabilities=caps)

# 把全局经验库指向临时文件，避免污染真实库
agent_exp = fresh_experience()
import backend.memory.experience as _exp_mod
_orig_global = _exp_mod.experience
_orig_root_ref = None


class FakeSlowLLM:
    """鸭子类型的假模型：返回一条**具体动作**（符合 Design B 的 Root 契约）。"""

    def __init__(self):
        self.available = True
        self.offline = False
        self.last_used_backend = "fake"
        self.calls = 0

    def chat_json(self, messages, **kw):
        self.calls += 1
        assert any("历史成功案例" in str(m.get("content", ""))
                   for m in messages) or True
        return {"is_command": True, "reply": "已为您调暗灯光。",
                "actions": [{"device_id": "light_living_01",
                             "command": "set_brightness", "params": {"brightness": 20}}]}


SLOW_INPUT = "把整个屋子的氛围弄温馨一点"      # 已验证会走到慢环（见 tests_budget.py）
fake = FakeSlowLLM()
agent.llm.chat_json = fake.chat_json
agent.llm.available = True
agent.root.llm = agent.llm

# 用临时经验库替换掉 root_agent 里引用的那一个
_exp_mod.experience = agent_exp
import backend.agents.root_agent as _root_mod
_root_mod.experience = agent_exp
import backend.agent as _agent_mod
_agent_mod.experience = agent_exp

try:
    check("前置：该输入确实走慢环（否则本节的断言全是假通过）",
          agent.root.plan(SLOW_INPUT, {}, [], allow_cloud=True).source == "llm")
    check("前置：经验库初始为空", agent_exp.stats()["total"] == 0)

    first = agent.decide(SLOW_INPUT)
    check("慢环执行成功", bool(first["actions"]), str(first["actions"]))
    check("★ 执行后写回了经验（闭环第 3 段）",
          agent_exp.stats()["total"] == 1, str(agent_exp.stats()["total"]))
    rec = agent_exp.retrieve(query=SLOW_INPUT, limit=1, min_rating=0)[0]
    check("★ 评分来自执行结果（全部成功 → 5）", rec["rating"] == 5, str(rec["rating"]))
    check("评分来源标注为 auto_execution（不是用户满意度）",
          rec["rating_source"] == "auto_execution", rec["rating_source"])
    check("上下文键已归纳为「房间|类别」",
          "客厅" in rec["context_key"] and "light" in rec["context_key"],
          rec["context_key"])
    check("规则命中的请求不写经验（否则经验库被开灯/关灯淹没）",
          (agent.decide("打开客厅灯"), agent_exp.stats()["total"])[1] == 1,
          str(agent_exp.stats()["total"]))

    second = agent.decide(SLOW_INPUT)
    check("★ 第二次同类请求注入了历史案例（闭环第 4 段）",
          any(t.get("step") == "recall" for t in second["decision_trace"]),
          str([t.get("step") for t in second["decision_trace"]]))
    recall = next(t for t in second["decision_trace"] if t.get("step") == "recall")
    check("注入的案例可复核（trace 里写明是哪一条、评分多少）",
          rec["id"] in recall["detail"] and "5/5" in recall["detail"], recall["detail"])
    check("经验库累积到 2 条（每次慢环都记）",
          agent_exp.stats()["total"] == 2, str(agent_exp.stats()["total"]))

    # 失败的经验不该被注入
    agent_exp.record("fuzzy_command", "客厅|light", "把整个屋子的氛围弄温馨一点",
                     [], "失败", rating=1)
    ctx = agent_exp.build_context(prefer_category="fuzzy_command",
                                  query="把整个屋子的氛围弄温馨一点")
    check("★ 失败案例不进 prompt（准入标准是执行成功，不是「发生过」）",
          "（执行结果 1/5）" not in ctx, ctx[-90:])
finally:
    _exp_mod.experience = _orig_global
    _root_mod.experience = _orig_global
    _agent_mod.experience = _orig_global

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
