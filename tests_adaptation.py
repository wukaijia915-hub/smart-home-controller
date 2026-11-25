# -*- coding: utf-8 -*-
"""L5 自治测试：偏好带 / 仲裁 / 四道闸 / 冷启动 / 漂移降级 / 安全设备排除。

对应申报书「L5 三项免除 + 可测判据」：
- 免配置：新房间靠偏好带自举，样本不足时只给区间不给建议；
- 免调参：四道闸（包络/步长/间隔/预算）全部来自配置；
- 免确认：只有 auto + auto 模式才自动执行，其它等级只提议；
- 安全：门锁/燃气/摄像头/窗户永不进入候选。
"""
import ast
import os
import sys
import tempfile
from pathlib import Path

_TMP_DIR = tempfile.mkdtemp(prefix="lx_adapt_")
os.environ["LINGXI_DATA_DIR"] = _TMP_DIR
os.environ["LLM_OFFLINE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.adaptation import (AdaptationPolicy, Proposal, arbitrate,
                                best_band, learn_bands)
from backend.adaptation.preference import hour_bucket
from backend.devices.registry import DeviceRegistry
from backend.state import StateStore

PASS, FAIL = 0, 0
T0 = 1_760_000_000.0

CFG = {
    "enabled": True,
    "autonomy_initial": "auto",
    "attributes": {
        "temperature": {"min": 16, "max": 30, "max_delta": 2, "min_interval_seconds": 1800},
        "target": {"min": 30, "max": 80, "max_delta": 10, "min_interval_seconds": 1800},
    },
    "daily_budget": 60,
    "cold_start_samples": 12,
    "probe_step_ratio": 0.5,
    "feedback_required": 12,
    "water_heater_hours": [6, 7, 20, 21],
    "comfort": {"temperature_margin": 0.5, "humidity_margin": 5},
    "drift": {"window_hours": 24, "correction_rate_max": 0.1,
              "comfort_hit_drop_points": 10, "degraded_ratio_max": 0.2},
    "safe_device_exclusions": ["lock", "gas_stove", "camera", "window"],
    "priority_classes": ["ac", "humidifier", "dehumidifier", "water_heater", "robot_vacuum"],
    "autonomy_levels": ["auto", "confirm", "suggest_only", "off"],
}


def check(label: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}  {detail}")


def entries(n: int, value: float = 24.0, device: str = "ac_living_01"):
    return [{"ok": True, "command": "set_temperature", "device_id": device,
             "params": {"temperature": value}, "ts": T0} for _ in range(n)]


def new_policy(cfg: dict | None = None) -> AdaptationPolicy:
    path = Path(tempfile.mkdtemp(prefix="lx_adapt_state_")) / "state.json"
    return AdaptationPolicy(cfg or dict(CFG), state_path=path)


def proposal(category="ac", command="set_temperature", params=None, attribute="temperature",
             value=26.0, device="ac_living_01", room="客厅"):
    return Proposal(proposal_id="p_" + device + "_" + command, device_id=device,
                    device_name=device, category=category, room=room,
                    command=command, params=params or {"temperature": value},
                    attribute=attribute, value=value)


print("=" * 60)
print("  犀鉴 — L5 自治测试（偏好带 / 仲裁 / 四道闸 / 冷启动 / 降级）")
print("=" * 60)

# ---------------------------------------------------------------- 1. 偏好带
print("\n== 1. 偏好带：分桶 / 冷启动 / 确定性 ==")
reg = DeviceRegistry()
_BUCKET = hour_bucket(T0)
bands = learn_bands(entries(12, 24.0), CFG, reg)
band = best_band(bands, "temperature", "客厅", _BUCKET)
check("12 个样本 → 非冷启动", band is not None and band.cold_start is False,
      str(band.to_dict() if band else None))
check("分位带落在样本值上", band.low == 24.0 and band.high == 24.0,
      f"{band.low}/{band.high}")
bands_cold = learn_bands(entries(3, 24.0), CFG, reg)
cold = next(iter(bands_cold.values()))
check("3 个样本 → cold_start=True（区间不是偏好）", cold.cold_start is True,
      str(cold.to_dict()))
bands2 = learn_bands(entries(12, 24.0), CFG, reg)
check("同输入两次学习结果一致（确定性）",
      {k: v.to_dict() for k, v in bands.items()} == {k: v.to_dict() for k, v in bands2.items()})
check("时段分桶覆盖四档",
      all(hour in ("night", "morning", "afternoon", "evening")
          for hour in {b.bucket for b in bands.values()}))

# ---------------------------------------------------------------- 2. 仲裁
print("\n== 2. 目标仲裁：舒适优先 / 方向冲突 / 安全排除 / 确定性 ==")
_hum_props = [
    proposal(category="humidifier", command="set_target", params={"target": 60},
             attribute="target", value=60, device="humidifier_living_01"),
    proposal(category="dehumidifier", command="set_target", params={"target": 45},
             attribute="target", value=45, device="dehumidifier_bedroom_01"),
]
res_wet = arbitrate(_hum_props, {"sensors": {"humidity": 70}}, CFG)
check("湿度 70% → 保留除湿、加湿被延后",
      [p.category for p in res_wet["chosen"]] == ["dehumidifier"]
      and len(res_wet["deferred"]) == 1, str(res_wet["chosen"]))
res_dry = arbitrate(_hum_props, {"sensors": {"humidity": 40}}, CFG)
check("湿度 40% → 保留加湿、除湿被延后",
      [p.category for p in res_dry["chosen"]] == ["humidifier"], str(res_dry["chosen"]))

_lock = proposal(category="lock", command="unlock", params={}, attribute="", value=None,
                 device="lock_entry_01")
res_safe = arbitrate([_lock], {}, CFG)
check("门锁永不进入 L5 候选", not res_safe["chosen"] and res_safe["deferred"])

_ordered = [proposal(device="ac_bedroom_01"), proposal(device="ac_living_01")]
r1 = arbitrate(list(_ordered), {}, CFG)
r2 = arbitrate(list(reversed(_ordered)), {}, CFG)
check("输入顺序不同、仲裁结果相同（可回放）",
      [p.device_id for p in r1["chosen"]] == [p.device_id for p in r2["chosen"]],
      f"{[p.device_id for p in r1['chosen']]} vs {[p.device_id for p in r2['chosen']]}")

# ---------------------------------------------------------------- 3. 四道闸
print("\n== 3. 四道闸：包络 / 步长 / 间隔 / 预算 ==")
policy = new_policy()
store = StateStore()
_p = proposal(params={"temperature": 99}, value=99)
policy._check_gates(_p, reg, store, T0)
check("闸①包络：99℃ 被拦", _p.gate_ok is False and "上限" in _p.gate_reason, _p.gate_reason)

_p = proposal(params={"temperature": 29}, value=29)
policy._check_gates(_p, reg, store, T0)
check("闸②步长：26→29 超过 2", _p.gate_ok is False and "步长" in _p.gate_reason, _p.gate_reason)

store.note_action("ac_living_01", T0)
_p = proposal(params={"temperature": 26}, value=26)
policy._check_gates(_p, reg, store, T0 + 10)
check("闸③间隔：10s 内第二次被拦", _p.gate_ok is False and "间隔" in _p.gate_reason, _p.gate_reason)

store2 = StateStore()
store2.note_action("ac_living_01", T0)
cfg_budget = dict(CFG, daily_budget=1)
policy_b = new_policy(cfg_budget)
_p = proposal(params={"temperature": 26}, value=26)
policy_b._check_gates(_p, reg, store2, T0 + 1)
check("闸④预算：当日第 2 次被拦", _p.gate_ok is False and "预算" in _p.gate_reason, _p.gate_reason)

# ---------------------------------------------------------------- 4. 提议与冷启动
print("\n== 4. 提议 / 冷启动 / 安全设备 ==")
policy = new_policy()
policy.state.level = "auto"
policy.state.mode = "shadow"
result = policy.propose({"sensors": {"temp_indoor": 28.0}}, entries(12), reg, StateStore(), T0)
check("影子模式：有提议但 executable=False",
      bool(result["proposals"]) and all(not p["executable"] for p in result["proposals"]))

policy.state.mode = "auto"
result = policy.propose({"sensors": {"temp_indoor": 28.0}}, entries(12), reg, StateStore(), T0)
check("auto 模式：提议可执行且带门控结果",
      any(p["executable"] and p["gate_ok"] for p in result["proposals"]),
      str([(p["command"], p["executable"], p["gate_ok"]) for p in result["proposals"]]))
check("提议不含安全设备",
      all(p["category"] not in ("lock", "gas_stove", "camera") for p in result["proposals"]))

policy.state.mode = "shadow"
policy.state.feedback = 0
policy.config["feedback_required"] = 20
check("冷启动：0 反馈仍是 shadow", policy.advance_mode() == "shadow")
policy.state.feedback = 12
check("冷启动：12 反馈进入 probe", policy.advance_mode() == "probe")
policy.state.feedback = 20
check("冷启动：20 反馈进入 auto", policy.advance_mode() == "auto")

# ---------------------------------------------------------------- 5. 漂移降级
print("\n== 5. 漂移降级：auto → confirm → suggest_only → off ==")
policy = new_policy()
policy.state.level = "auto"
_d = policy.assess_drift({"correction_rate": 0.5})
check("纠正率超限 → 退到 confirm 且回影子模式",
      _d["degraded"] and policy.state.level == "confirm" and policy.state.mode == "shadow",
      str(_d))
_d = policy.assess_drift({"comfort_hit_drop_points": 20})
check("舒适带下降超限 → 退到 suggest_only", policy.state.level == "suggest_only", str(_d))
_d = policy.assess_drift({"degraded_ratio": 0.9})
check("感知降级比例超限 → 退到 off", policy.state.level == "off", str(_d))
check("无越限时不降级", new_policy().assess_drift({})["degraded"] is False)

# ---------------------------------------------------------------- 6. 执行链路
print("\n== 6. 执行：L5 动作走唯一执行入口 ==")
policy = new_policy()
policy.state.level = "auto"
policy.state.mode = "auto"
store = StateStore()
result = policy.propose({"sensors": {"temp_indoor": 28.0}}, entries(12), reg, store, T0)
executable = [p for p in policy.last_proposals if p.executable]
check("存在可执行提议", bool(executable), str(len(policy.last_proposals)))
out = policy.apply(policy.last_proposals, reg, store, T0)
check("apply 返回 decision_id 且执行成功",
      out.get("ok") and out.get("decision_id"), str(out)[:120])
check("审计里记录来源 adaptation",
      any(e.get("source") == "adaptation" for e in store.get_audit(20)))
check("执行后设备进入目标态",
      reg.get("ac_living_01").get("on") is True, str(reg.get("ac_living_01").get("on")))

# ---------------------------------------------------------------- 7. 静态不变量
print("\n== 7. 静态不变量：自治层不直接执行 ==")
root = Path(__file__).resolve().parent
offenders = []
for py in (root / "backend" / "adaptation").glob("*.py"):
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "control"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "registry"):
            offenders.append(py.name)
check("adaptation/ 不含 registry.control 调用", not offenders, str(offenders))

# ---------------------------------------------------------------- 汇总
print()
print("=" * 60)
print(f"  结果: {PASS} passed, {FAIL} failed")
print("=" * 60)
sys.exit(1 if FAIL else 0)
