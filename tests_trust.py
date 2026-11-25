# -*- coding: utf-8 -*-
"""信任层测试：六段因果链 / 五层归因 / 四道闸 / 撤销 / 静态不变量。

对应申报书「信任层」的四条对外承诺：
- 每条决策可查：signal → inference → gate → lease → action → readback；
- 失败可定位：sensor / inference / gate / lease / device / readback；
- 可逆动作 10 分钟内可撤销，撤销前做漂移校验；
- 门锁、燃气、摄像头直播等高危动作永不自动执行，也不生成撤销记录。
"""
import ast
import os
import sys
import tempfile
from pathlib import Path

_TMP_DIR = tempfile.mkdtemp(prefix="lx_trust_")
os.environ["LINGXI_DATA_DIR"] = _TMP_DIR
os.environ["LLM_OFFLINE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import execution
from backend.agent import HomeAgent
from backend.config_loader import load_adaptation, load_trust
from backend.devices.registry import DeviceRegistry
from backend.lease import Lease
from backend.state import StateStore
from backend.trust import apply_undo, can_undo, explain, explain_recent, undo_store
from backend.trust.explain import attribute_failure

PASS, FAIL = 0, 0
T0 = 1_760_000_000.0


def check(label: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}  {detail}")


print("=" * 60)
print("  犀鉴 — 信任层测试（因果链 / 归因 / 四道闸 / 撤销）")
print("=" * 60)

# ---------------------------------------------------------------- 1. 六段链
print("\n== 1. 六段因果链：一次对话能查到底 ==")
reg = DeviceRegistry()
agent = HomeAgent(reg)
res = agent.decide("空调调到24度")
decision_id = res.get("decision_id", "")
check("decide 返回 decision_id", bool(decision_id), str(decision_id))

detail = explain(agent.store, decision_id)
check("explain 能取到链条", detail["ok"] is True, str(detail)[:80])
stages = [s.get("stage") for s in detail.get("stages", [])]
for stage in ("signal", "inference", "gate", "lease", "action", "readback"):
    check(f"链上含 {stage} 段", stage in stages, str(stages))
check("动作条目挂在同一 decision_id 上",
      bool(detail.get("actions")) and all(a.get("decision_id") == decision_id
                                         for a in detail["actions"]),
      str([a.get("decision_id") for a in detail.get("actions", [])]))
check("成功决策的 failed_layer 为空", detail.get("failed_layer") == "", str(detail.get("failed_layer")))
check("recent 列表能枚举到这条决策",
      any(d["decision_id"] == decision_id for d in explain_recent(agent.store, 10)))

# ---------------------------------------------------------------- 2. 五层归因
print("\n== 2. 失败归因：能指到具体一层 ==")
_r = execution.execute_action("ac_living_01", "set_temperature", {"temperature": 99},
                              registry=reg, store=agent.store)
check("包络越界 → gate 层", _r.get("failed_layer") == "gate", str(_r))
check("拒绝原因写明上限", "上限" in str(_r.get("error")), str(_r.get("error")))

_expired = Lease.issue("light_living_01", "turn_on", {}, "rule", 0, "light", ttl=-1)
_r = execution.execute_action("light_living_01", "turn_on", {}, registry=reg,
                              store=agent.store, lease=_expired)
check("租约过期 → lease 层", _r.get("failed_layer") == "lease", str(_r))

reg.set_state("light_living_01", "offline")
_r = execution.execute_action("light_living_01", "turn_on", {}, registry=reg,
                              store=agent.store)
check("设备离线 → device 层", _r.get("failed_layer") == "device", str(_r))
reg.set_state("light_living_01", "online")

_orig_readback = reg._readback
reg._readback = lambda device, command, params, settings=None: {
    "attribute": "on", "expected": True, "actual": False, "confirmed": False,
    "trusted": True, "reason": "回读不一致：期望 True，实际 False", "checked_at": 0}
_r = execution.execute_action("light_living_01", "turn_on", {}, registry=reg,
                              store=agent.store)
reg._readback = _orig_readback
check("回读不符 → delivered + readback 层",
      _r.get("status") == "delivered" and _r.get("failed_layer") == "readback", str(_r))

check("attribute_failure 能识别 device / readback",
      attribute_failure({"ok": False}) == "device"
      and attribute_failure({"ok": True, "status": "delivered"}) == "readback")
check("审计里保留 before / after / readback",
      all({"before", "after", "readback"} <= set(e) for e in agent.store.get_audit(20)))

# ---------------------------------------------------------------- 3. 四道闸
print("\n== 3. 四道闸：包络 / 步长 / 频率 / 当日预算 ==")
cfg = load_adaptation()
store = StateStore()
reg = DeviceRegistry()

_r = execution.execute_action("ac_living_01", "set_temperature", {"temperature": 99},
                              registry=reg, store=store, bounds=cfg, now=T0)
check("闸①包络：99℃ 被拦", _r.get("status") == "rejected", str(_r))

_r = execution.execute_action("ac_living_01", "set_temperature", {"temperature": 29},
                              registry=reg, store=store,
                              bounds={"attributes": {"temperature": {"min": 16, "max": 30,
                                                                     "max_delta": 2}}},
                              now=T0)
check("闸②步长：26→29 超过 max_delta=2", _r.get("status") == "rejected"
      and "步长" in str(_r.get("error")), str(_r.get("error")))

_b = {"attributes": {"temperature": {"min": 16, "max": 30, "min_interval_seconds": 1800}}}
_r1 = execution.execute_action("ac_living_01", "set_temperature", {"temperature": 26},
                               registry=reg, store=store, bounds=_b, now=T0)
_r2 = execution.execute_action("ac_living_01", "set_temperature", {"temperature": 27},
                               registry=reg, store=store, bounds=_b, now=T0 + 10)
check("闸③频率：10s 内第二次被拦", _r1.get("ok") and _r2.get("status") == "rejected"
      and "间隔" in str(_r2.get("error")), str(_r2.get("error")))

_b2 = {"attributes": {"temperature": {"min": 16, "max": 30}}, "daily_budget": 1}
store2 = StateStore()
reg2 = DeviceRegistry()
_r1 = execution.execute_action("ac_living_01", "set_temperature", {"temperature": 26},
                               registry=reg2, store=store2, bounds=_b2, now=T0)
_r2 = execution.execute_action("ac_living_01", "set_temperature", {"temperature": 27},
                               registry=reg2, store=store2, bounds=_b2, now=T0 + 1)
check("闸④预算：当日第 2 次被拦", _r1.get("ok") and _r2.get("status") == "rejected"
      and "预算" in str(_r2.get("error")), str(_r2.get("error")))

# ---------------------------------------------------------------- 4. 撤销
print("\n== 4. 撤销：5 类可逆动作 + 窗口 + 漂移 ==")
reg3 = DeviceRegistry()
store3 = StateStore()
cases = [
    ("light_living_01", "set_brightness", {"brightness": 60}, "brightness", 0),
    ("ac_living_01", "set_temperature", {"temperature": 24}, "temperature", 26),
    ("curtain_living_01", "set_position", {"position": 70}, "position", 0),
    ("tv_living_01", "set_volume", {"volume": 30}, "volume", 20),
    ("fan_living_01", "set_speed", {"speed": "high"}, "speed", 2),
]
undone_ok = 0
for device_id, command, params, attr, expected in cases:
    before = reg3.get(device_id).get(attr)
    r = execution.execute_action(device_id, command, params, registry=reg3,
                                 store=store3, source="rule", now=T0)
    ok_action = r.get("ok") and r.get("action_id")
    check(f"执行 {device_id}.{command}", bool(ok_action), str(r))
    if not ok_action:
        continue
    chk = can_undo(r["action_id"], reg3, now=T0 + 10)
    check(f"可撤销 {device_id}.{command}", chk.get("ok") is True, str(chk.get("reason")))
    out = apply_undo(r["action_id"], registry=reg3, store=store3, now=T0 + 10)
    restored = reg3.get(device_id).get(attr) == before
    check(f"撤销后 {attr} 恢复 {before!r}", out.get("ok") and restored,
          f"out={out.get('error')} now={reg3.get(device_id).get(attr)!r}")
    if out.get("ok") and restored:
        undone_ok += 1
check("5 类可逆动作撤销全部成功（无状态漂移）", undone_ok == len(cases), f"{undone_ok}/{len(cases)}")

# 窗口过期
_r = execution.execute_action("light_living_01", "turn_on", {}, registry=reg3,
                              store=store3, now=T0)
_chk = can_undo(_r["action_id"], reg3, now=T0 + 601)
check("超过 600s 窗口 → 拒绝撤销", _chk.get("ok") is False and "窗口" in str(_chk.get("reason")),
      str(_chk.get("reason")))

# 漂移：动作后设备被手动改过
_r = execution.execute_action("light_living_01", "set_brightness", {"brightness": 70},
                              registry=reg3, store=store3, now=T0)
reg3.get("light_living_01").set("brightness", 35)
_chk = can_undo(_r["action_id"], reg3, now=T0 + 10)
check("设备当前值被改动 → 拒绝覆盖（漂移校验）",
      _chk.get("ok") is False and "改动" in str(_chk.get("reason")), str(_chk.get("reason")))

# 高危：拒绝执行，且不生成撤销记录
_r = execution.execute_action("lock_entry_01", "unlock", {}, registry=reg3,
                              store=store3, now=T0)
check("高危自动路径被拒（gate）", _r.get("status") == "rejected", str(_r))
check("高危动作不生成撤销记录", undo_store.get(_r.get("action_id", "")) is None, str(_r))

# ---------------------------------------------------------------- 5. 审计滚出
print("\n== 5. 审计滚出环形缓冲：如实返回 None ==")
_small = StateStore(max_decisions=2)
for i in range(3):
    _small.open_decision(f"dec_{i}", "rule")
_rep = explain(_small, "dec_0")
check("滚出的决策返回 ok=False", _rep.get("ok") is False, str(_rep)[:80])
check("原因写明滚出缓冲", "滚出" in str(_rep.get("reason")), str(_rep.get("reason")))

# ---------------------------------------------------------------- 6. 静态不变量
print("\n== 6. 静态不变量：唯一写入口 ==")
root = Path(__file__).resolve().parent
offenders = []
for py in (root / "backend").rglob("*.py"):
    if "__pycache__" in str(py):
        continue
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except Exception:
        continue
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "control"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "registry"):
            offenders.append(py.relative_to(root).as_posix())
check("只有 execution.py 调 registry.control（其余模块为零）",
      set(offenders) <= {"backend/execution.py"}, str(sorted(set(offenders))))

for py in (root / "backend" / "trust").glob("*.py"):
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except Exception:
        continue
    trust_offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "control"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "registry"):
            trust_offenders.append(py.name)
    check(f"trust/{py.name} 不含 registry.control 调用", not trust_offenders,
          str(trust_offenders))

# ---------------------------------------------------------------- 汇总
print()
print("=" * 60)
print(f"  结果: {PASS} passed, {FAIL} failed")
print("=" * 60)
sys.exit(1 if FAIL else 0)
