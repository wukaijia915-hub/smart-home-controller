# -*- coding: utf-8 -*-
"""感知层测试：融合算法、门控、以及与执行层的同构关系。

为什么要专门测「感知结果不产生设备动作」：
这是一条**架构承诺**，靠代码评审守不住 —— 半年后有人在 perception/ 里加一句
`registry.control(...)` 实现"自动关怀"，谁也不会注意到。所以这里用静态检查把它
变成会失败的测试。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.gate import Gate, GateResult
from backend.lease import Lease, LeaseGate, LeaseValidator
from backend.perception import (
    MIN_FUSED_CONFIDENCE,
    MODAL_DISTANCE_THRESHOLD,
    PERCEPTION_CANNOT_ACTUATE,
    ModalReading,
    MultiModalFuser,
    MultiModalGate,
    classify_emotion_7class,
    entropy_confidence,
    stability_confidence,
)

PASSED, FAILED = 0, 0


def check(label: str, condition: bool, detail: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {label}")
    else:
        FAILED += 1
        print(f"  ✗ {label}  {detail}")


print("=" * 60)
print("  灵犀 — 感知层测试")
print("=" * 60)

# ---------------------------------------------------------------- 1. 心镜原公式
print("\n== 1. 移植公式与心镜原实现一致 ==")

uniform = {k: 1 / 7 for k in "abcdefg"}
peaked = {"a": 0.98, "b": 0.01, "c": 0.004, "d": 0.003, "e": 0.001, "f": 0.001, "g": 0.001}

check("熵置信度：均匀分布 → 0", abs(entropy_confidence(uniform)) < 1e-9,
      str(entropy_confidence(uniform)))
check("熵置信度：尖锐分布 → 接近 1", entropy_confidence(peaked) > 0.9,
      str(entropy_confidence(peaked)))
check("熵置信度：空分布 → 0", entropy_confidence({}) == 0.0)
check("熵置信度：单类别 → 1", entropy_confidence({"a": 1.0}) == 1.0)
check("熵置信度：结果落在 [0,1]",
      0.0 <= entropy_confidence(peaked) <= 1.0)

stable = [{"s": 10}, {"s": 10}, {"s": 10}]
unstable = [{"s": 10}, {"s": 40}, {"s": 70}]
check("稳定性置信度：完全稳定 → 1", abs(stability_confidence(stable, ["s"]) - 1.0) < 1e-9)
check("稳定性置信度：剧烈抖动 → 0 附近", stability_confidence(unstable, ["s"]) < 0.1,
      str(stability_confidence(unstable, ["s"])))
check("稳定性置信度：无窗口 → 0", stability_confidence([], ["s"]) == 0.0)
check("稳定性置信度：单窗口 → 0.5（心镜原语义）",
      stability_confidence([{"s": 1}], ["s"]) == 0.5)

check("V/A → 7 类标签：正效价高唤醒 → 开心",
      classify_emotion_7class(0.8, 0.6) == "开心")
check("V/A → 7 类标签：原点 → 中性", classify_emotion_7class(0.0, 0.0) == "中性")

# ---------------------------------------------------------------- 2. N 模态推广
print("\n== 2. 二模态 → N 模态推广 ==")

fuser = MultiModalFuser()

# 两模态应与心镜等价：权重按置信度分配
out2 = fuser.fuse([ModalReading("a", 1.0, 1.0, 0.8), ModalReading("b", -1.0, -1.0, 0.2)])
check("两模态：权重 = 置信度归一化",
      abs(out2.weights["a"] - 0.8) < 1e-9 and abs(out2.weights["b"] - 0.2) < 1e-9,
      str(out2.weights))
check("两模态：融合值 = 加权平均",
      abs(out2.valence - (0.8 * 1.0 + 0.2 * -1.0)) < 1e-9, str(out2.valence))
check("两模态：融合置信度 = 两者平均",
      abs(out2.confidence - 0.5) < 1e-9, str(out2.confidence))

out3 = fuser.fuse([
    ModalReading("a", 0.5, 0.6, 0.8),
    ModalReading("b", 0.6, 0.5, 0.7),
    ModalReading("c", 0.45, 0.65, 0.6),
])
check("三模态：权重和为 1", abs(sum(out3.weights.values()) - 1.0) < 1e-9,
      str(sum(out3.weights.values())))
check("三模态：一致性复核取最大两两距离", out3.modal_distance > 0 and out3.consistent)

# ---------------------------------------------------------------- 3. 冲突拒绝
print("\n== 3. 模态冲突 → 拒绝（不降级使用）==")

gate = MultiModalGate()
conflict = [
    ModalReading("vision_behavior", 0.9, 0.8, 0.9, detail={"person": "present"}),
    ModalReading("facial_expression", -0.8, 0.2, 0.9, detail={"expr": "angry"}),
]
g = gate.evaluate(conflict)
check("冲突时 passed=False", g.passed is False)
check("冲突判定距离超过阈值",
      g.evidence["modal_distance"] > MODAL_DISTANCE_THRESHOLD,
      str(g.evidence["modal_distance"]))
check("拒绝原因说明是哪个模态对冲突",
      "vision_behavior" in g.reason and "facial_expression" in g.reason, g.reason)
check("evidence 保留各模态原始读数（可追查）",
      len(g.evidence.get("contributions", [])) == 2,
      str(len(g.evidence.get("contributions", []))))

# 冲突阈值边界
edge_ok = [
    ModalReading("a", 0.0, 0.0, 0.9),
    ModalReading("b", MODAL_DISTANCE_THRESHOLD * 0.99, 0.0, 0.9),
]
edge_bad = [
    ModalReading("a", 0.0, 0.0, 0.9),
    ModalReading("b", MODAL_DISTANCE_THRESHOLD * 1.01, 0.0, 0.9),
]
check("恰好低于阈值 → 通过", gate.evaluate(edge_ok).passed is True)
check("恰好超过阈值 → 拒绝", gate.evaluate(edge_bad).passed is False)

# ---------------------------------------------------------------- 4. 零信息不制造中间值
print("\n== 4. 零信息 → 拒绝，且不制造 0.5（有意偏离心镜）==")

zero = gate.evaluate([ModalReading("m1", 0.5, 0.5, 0.0), ModalReading("m2", 0.5, 0.5, 0.0)])
check("全部零置信度 → passed=False", zero.passed is False)
check("融合置信度为 0（不是心镜的 0.5）", zero.confidence == 0.0, str(zero.confidence))
check("拒绝原因说明未制造中间值", "不制造" in zero.reason, zero.reason)

low = gate.evaluate([ModalReading("m1", 0.5, 0.5, 0.1), ModalReading("m2", 0.5, 0.5, 0.1)])
check("置信度低于下限 → 拒绝", low.passed is False and low.confidence < MIN_FUSED_CONFIDENCE,
      f"conf={low.confidence}")

check("无读数 → 拒绝", gate.evaluate([]).passed is False)
check("全 None → 拒绝", gate.evaluate([None, None]).passed is False)

# ---------------------------------------------------------------- 5. 端侧处理标记
print("\n== 5. 端侧处理（原始画面/音频不出户）==")

local = gate.evaluate([ModalReading("a", 0.5, 0.5, 0.9), ModalReading("b", 0.5, 0.5, 0.9)])
check("全部端侧处理 → local_only=True", local.evidence["local_only"] is True)

remote = gate.evaluate([
    ModalReading("a", 0.5, 0.5, 0.9, local_only=True),
    ModalReading("b", 0.5, 0.5, 0.9, local_only=False),
])
check("有模态非端侧 → local_only=False（不掩饰）", remote.evidence["local_only"] is False)

# ---------------------------------------------------------------- 6. 与执行层同构
print("\n== 6. 感知层与执行层共用同一套门控抽象 ==")

check("MultiModalGate 实现 Gate 接口", isinstance(MultiModalGate(), Gate))
check("LeaseGate 实现 Gate 接口", isinstance(LeaseGate(), Gate))
check("两者的 evaluate 都返回 GateResult",
      isinstance(gate.evaluate([ModalReading("a", 0.5, 0.5, 0.9)]), GateResult)
      and isinstance(LeaseGate().evaluate({"lease": Lease.issue(
          "light_living_01", "turn_on", {}, "rule", 0, "light"), "current_version": 0}),
          GateResult))

# 租约门控：正常租约通过、版本失配拒绝、高危挂起
lg = LeaseGate()
ok_lease = Lease.issue("light_living_01", "turn_on", {}, "rule", 5, "light")
check("租约有效 → passed=True",
      lg.evaluate({"lease": ok_lease, "current_version": 5}).passed is True)
check("版本失配 → passed=False",
      lg.evaluate({"lease": ok_lease, "current_version": 6}).passed is False)
check("subject 格式错误 → passed=False（不抛异常）",
      lg.evaluate("not a dict").passed is False)

high = Lease.issue("lock_entry_01", "unlock", {}, "rule", 0, "lock")
hg = lg.evaluate({"lease": high, "current_version": 0})
check("高危操作 → 门控不通过且要求二次确认",
      hg.passed is False and hg.evidence.get("requires_confirmation") is True,
      str(hg.evidence))

# ---------------------------------------------------------------- 7. 硬边界（静态检查）
print("\n== 7. 硬边界：感知层不得有执行设备的能力 ==")

check("PERCEPTION_CANNOT_ACTUATE 为 True", PERCEPTION_CANNOT_ACTUATE is True)

PERCEPTION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend", "perception")
violations = []
for root, _dirs, files in os.walk(PERCEPTION_DIR):
    for name in files:
        if not name.endswith(".py"):
            continue
        path = os.path.join(root, name)
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                code = line.split("#", 1)[0]        # 忽略注释
                if re.search(r"\.control\s*\(", code):
                    violations.append(f"{name}:{lineno} 调用了 registry.control")
                if re.search(r"\bLease\.issue\b", code):
                    violations.append(f"{name}:{lineno} 构造了租约")

check("perception/ 下无设备控制调用、无租约构造",
      not violations, "; ".join(violations))
check("perception/ 不导入 DeviceRegistry 或 lease",
      not any(re.search(r"import.*(DeviceRegistry|lease)", open(
          os.path.join(PERCEPTION_DIR, n), encoding="utf-8").read())
          for n in os.listdir(PERCEPTION_DIR) if n.endswith(".py")),
      "有违反（感知层不应知道设备注册表与租约的存在）")

# ---------------------------------------------------------------- 8. 感知服务
print("\n== 8. 感知服务：模态 → 融合 → 门控 → 询问式建议 ==")

from backend.perception import (
    EnvironmentBehaviorModality,
    FacialExpressionModality,
    PerceptionContext,
    PerceptionService,
    VestibularModality,
    VisionBehaviorModality,
)
from backend.perception.service import ROUTED_ELSEWHERE

svc = PerceptionService()

# 无任何输入 → 不编造读数、不给建议
empty = svc.perceive(PerceptionContext())
check("无输入：无读数", len(empty.readings) == 0, str(len(empty.readings)))
check("无输入：门控不通过", empty.gate["passed"] is False)
check("无输入：不给建议", empty.suggestion is None)

# 舒适环境 + 有人在 → 平稳，不打扰
comfortable = PerceptionContext(
    sensors={"temp_indoor": 23.0, "humidity": 50.0, "air_quality": 85.0},
    recent_actions=[{"device_id": "light_living_01", "command": "turn_on"}] * 3,
    habit={"routine_deviation": 0.2},
    presence_minutes=120.0,          # 家中有人，否则会被判为 away
)
res = svc.perceive(comfortable)
check("舒适环境：产出读数", len(res.readings) >= 1, str([r.modality for r in res.readings]))
check("舒适环境：门控通过", res.gate["passed"] is True, res.gate.get("reason", ""))
check("舒适环境：判定为正常居住", res.situation["region"] == "normal",
      res.situation["region"])
check("舒适环境：状态平稳不打扰", res.suggestion is None, str(res.suggestion))
check("舒适环境：给出了依据说明", bool(res.suggestion_basis), res.suggestion_basis)

# 不适环境 + 有人在 → 应给出**问句**
uncomfortable = PerceptionContext(
    sensors={"temp_indoor": 32.0, "humidity": 75.0, "air_quality": 30.0},
    recent_actions=[],
    habit={"routine_deviation": 0.5},
    presence_minutes=120.0,
    idle_minutes=10.0,
)
res2 = svc.perceive(uncomfortable)
check("不适环境：门控通过", res2.gate["passed"] is True, res2.gate.get("reason", ""))
check("不适环境：给出建议", res2.suggestion is not None, res2.suggestion_basis)
check("建议必须是问句（不能是指令）",
      res2.suggestion is not None and ("？" in res2.suggestion or "吗" in res2.suggestion),
      str(res2.suggestion))
check("建议不含命令式动词（不得出现设备指令措辞）",
      res2.suggestion is not None
      and not any(w in res2.suggestion for w in ["已为您", "已开启", "已关闭", "立即"]),
      str(res2.suggestion))
check("不适环境被情境层识别",
      res2.situation["region"] in ("discomfort", "anomaly"), res2.situation["region"])

# 紧急事件 → 交由事件路由，感知层不重复
fall = svc.perceive(PerceptionContext(scene_hint="跌倒",
                                      sensors={"temp_indoor": 23.0}))
has_fall = any(r.detail.get("event_type") in ROUTED_ELSEWHERE for r in fall.readings)
check("跌倒事件被识别为已路由事件", has_fall,
      str([r.detail.get("event_type") for r in fall.readings]))
check("跌倒不给重复建议（交事件路由）", fall.suggestion is None, str(fall.suggestion))
check("跌倒时说明了不重复的理由", "事件路由" in fall.suggestion_basis, fall.suggestion_basis)

# 模态冲突 → 不采信、不给建议
conflict_svc = PerceptionService(modalities=[
    VisionBehaviorModality(),
    EnvironmentBehaviorModality(),
])
# 注意：情境层与多模态门控相互独立 —— 情境是事实归纳（有真值来源），
# 门控管的是"多模态氛围判断是否自相矛盾"。所以要让"门控失败导致不出建议"
# 这条可测，情境必须同时是正常的，否则情境建议会先命中（那是正确行为）。
conflict_ctx = PerceptionContext(
    scene_hint="无人",                       # 视觉：无人在位（低唤醒）
    sensors={"temp_indoor": 23.0, "humidity": 50.0, "air_quality": 85.0},
    recent_actions=[{"a": 1}] * 8,           # 环境：操作频繁（高唤醒）→ 与视觉矛盾
    habit={"routine_deviation": 1.0},
    presence_minutes=120.0,
    idle_minutes=5.0,
)
cres = conflict_svc.perceive(conflict_ctx)
check("模态冲突时门控不通过", cres.gate["passed"] is False,
      f"距离={cres.gate.get('evidence', {}).get('modal_distance')}")
check("（前提）该输入的情境判定不覆盖冲突场景",
      cres.situation["region"] in ("normal",), cres.situation["region"])
check("模态冲突时不给氛围建议", cres.suggestion is None, str(cres.suggestion))
check("冲突说明写进了 note", bool(cres.note), cres.note)

# 反过来：情境异常时**不受**多模态门控失败影响（它有独立的真值来源）
conflict_discomfort = conflict_svc.perceive(PerceptionContext(
    scene_hint="有人",
    sensors={"temp_indoor": 35.0, "humidity": 80.0, "air_quality": 20.0},
    recent_actions=[{"a": 1}] * 5,
    habit={"routine_deviation": 1.0},
    presence_minutes=120.0,
    idle_minutes=5.0,
))
check("情境异常不因多模态冲突而被压掉（独立证据）",
      conflict_discomfort.situation["region"] == "discomfort"
      and conflict_discomfort.gate["passed"] is False
      and conflict_discomfort.suggestion is not None,
      f"region={conflict_discomfort.situation['region']} "
      f"gate={conflict_discomfort.gate['passed']}")

# 可选模态不可用 → 优雅降级，不编造
face = FacialExpressionModality()
check("面部模态可用性由依赖决定", isinstance(face.available(), bool))
check("无画面帧时面部模态返回 None",
      face.detect(PerceptionContext(face_frame=None)) is None)

vest = VestibularModality()
check("无前庭数据时返回 None", vest.detect(PerceptionContext()) is None)
check("有前庭数据时产出读数（含 ICC 警示）",
      (lambda r: r is not None and "ICC" in r.detail.get("caveat", ""))(
          vest.detect(PerceptionContext(vestibular={
              "valence": 0.4, "arousal": 0.3,
              "window_results": [{"stress": 30, "tension": 30}] * 3,
          }))))

# 假 analyzer 注入 → 面部模态可控可测
fake_face = FacialExpressionModality(analyzer=lambda f: {
    "emotion": {"happy": 0.85, "neutral": 0.10, "sad": 0.05}})
fr = fake_face.detect(PerceptionContext(face_frame="dummy"))
check("面部模态：注入 analyzer 后产出读数", fr is not None)
check("面部模态：熵置信度高（分布集中）", fr.confidence > 0.5, str(fr.confidence))
check("面部模态：效价为正（happy 占优）", fr.valence > 0, str(fr.valence))
check("面部模态：声明画面零上传", fr.detail.get("frame_bytes_uploaded") == 0)

# 服务状态可展示
st = svc.status()
check("服务状态含全部模态", len(st["modalities"]) >= 3, str(len(st["modalities"])))
check("服务状态含门控阈值", "distance_threshold" in st)
check("服务状态含情境参数清单", len(st["situation_params"]) == 5, str(st["situation_params"]))

# ---------------------------------------------------------------- 9. 白盒情境参数
print("\n== 9. 白盒情境参数：可解释、可复核 ==")

from datetime import datetime

from backend.perception import PresenceTracker, SituationAssessor, SituationInput

assessor = SituationAssessor()

# 参数齐全 + 每个参数都带真值来源与原始依据
idle_ctx = SituationInput(
    sensors={"temp_indoor": 23.0, "humidity": 50.0, "air_quality": 85.0},
    recent_actions=[{"a": 1}] * 5,
    presence_minutes=120.0,
    idle_minutes=10.0,
    now=datetime(2026, 10, 2, 14, 0),
)
state = assessor.assess(idle_ctx)
check("产出 5 个情境参数", len(state.params) == 5, str(list(state.params.keys())))
check("每个参数都有真值来源", all(p.truth for p in state.params.values()),
      str([k for k, p in state.params.items() if not p.truth]))
check("每个参数都有原始依据", all(p.evidence for p in state.params.values()),
      str([k for k, p in state.params.items() if not p.evidence]))
check("每个参数的归一化值落在 [0,1]",
      all(0.0 <= p.normalized <= 1.0 for p in state.params.values()))
check("正常环境判定为正常居住", state.region == "normal", state.region)

# 环境不适 → discomfort
hot = assessor.assess(SituationInput(
    sensors={"temp_indoor": 34.0, "humidity": 80.0, "air_quality": 25.0},
    presence_minutes=120.0, now=datetime(2026, 10, 2, 14, 0),
))
check("环境不适 → discomfort", hot.region == "discomfort", hot.region)
check("不适时舒适度偏差为正", hot.params["comfort_offset"].value > 0,
      str(hot.params["comfort_offset"].value))

# 深夜活动 → anomaly
night = assessor.assess(SituationInput(
    sensors={"temp_indoor": 23.0},
    recent_actions=[{"a": 1}] * 5,
    presence_minutes=120.0, idle_minutes=1.0,
    now=datetime(2026, 10, 2, 2, 0),          # 凌晨 2 点
))
check("深夜活动 → anomaly", night.region == "anomaly", night.region)
check("深夜活动被记入异常依据",
      "late_night_activity" in night.params["anomaly_count"].evidence,
      str(night.params["anomaly_count"].evidence))

# 久无活动 → prolonged_inactive
idle = assessor.assess(SituationInput(
    sensors={"temp_indoor": 23.0},
    recent_actions=[],
    presence_minutes=200.0, idle_minutes=200.0,
    now=datetime(2026, 10, 2, 14, 0),
))
check("久无活动 → prolonged_inactive", idle.region == "prolonged_inactive", idle.region)

# 无人 → away
away = assessor.assess(SituationInput(sensors={"temp_indoor": 23.0}, presence_minutes=0.0,
                                      now=datetime(2026, 10, 2, 14, 0)))
check("无人在位 → away", away.region == "away", away.region)

# 无历史数据 → 作息偏离度取中性值**并降低置信度**（不假装知道）
no_hist = assessor.assess(SituationInput(sensors={"temp_indoor": 23.0}, presence_minutes=120.0,
                                         now=datetime(2026, 10, 2, 14, 0)))
rd = no_hist.params["routine_deviation"]
check("无历史基线时作息偏离度置信度降低", rd.confidence < 1.0, str(rd.confidence))
check("无历史基线时明确标注", bool(rd.note), rd.note)

# 声明式配置：改配置即可改判定阈值，不用改代码
check("情境规则来自配置文件", bool(assessor.config.get("regions")),
      "配置未加载")
check("兜底规则在最后且条件为空",
      assessor.config["regions"][-1]["id"] == "normal"
      and not assessor.config["regions"][-1]["when"])

# PresenceTracker：时长为累积量，首次调用不凭空多出时长
tracker = PresenceTracker()
t0 = datetime(2026, 10, 2, 10, 0)
check("首次调用累计为 0", tracker.update(True, t0) == 0.0, str(tracker.minutes))
tracker.update(True, datetime(2026, 10, 2, 10, 10))
check("10 分钟后累计 10 分钟", abs(tracker.minutes - 10.0) < 0.01, str(tracker.minutes))
tracker.update(False, datetime(2026, 10, 2, 10, 30))
check("不在位期间不累计", abs(tracker.minutes - 10.0) < 0.01, str(tracker.minutes))
tracker.reset()
check("reset 后归零", tracker.minutes == 0.0 and tracker._last_ts is None)

# ---------------------------------------------------------------- 汇总
print()
print("=" * 60)
total = PASSED + FAILED
print(f"  结果: {PASSED} passed, {FAILED} failed  (共 {total} 项)")
print("=" * 60)

sys.exit(1 if FAILED else 0)
