# -*- coding: utf-8 -*-
"""多智能体改造验证：授权租约 / 幻觉拦截 / 高危二次确认 / 非法动作校验。

不依赖真实硬件，也不依赖 LLM 稳定性——高危挂起与非法拦截用 rule 子任务直达 Manager 校验层。
"""
import ast
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

from backend.devices.registry import DeviceRegistry
from backend.agent import HomeAgent
from backend.lease import Lease, LeaseValidator
from backend.agents import SubTask, TaskPlan


def test_lease_validator():
    # 过期租约拒绝
    lease = Lease.issue("light_living_01", "turn_on", {}, "rule", 0, "light", ttl=-1)
    ok, msg = LeaseValidator.verify(lease, 0)
    assert not ok and "过期" in msg, msg

    # 状态版本不匹配拒绝（重放/过期指令）
    lease2 = Lease.issue("light_living_01", "turn_on", {}, "rule", 1, "light", ttl=10)
    ok2, msg2 = LeaseValidator.verify(lease2, 3)
    assert not ok2 and "版本" in msg2, msg2

    # 正常通过
    lease3 = Lease.issue("light_living_01", "turn_on", {}, "rule", 2, "light", ttl=10)
    ok3, msg3 = LeaseValidator.verify(lease3, 2)
    assert ok3, msg3
    print("✓ 租约校验：过期拒绝 / 状态版本不匹配拒绝 / 正常通过")


def test_high_risk_suspend():
    reg = DeviceRegistry()
    agent = HomeAgent(reg)
    plan = TaskPlan(
        is_command=True, reply="test", state_version=agent.store.version, source="rule",
        sub_tasks=[SubTask(source="rule", device_id="lock_entry_01", command="unlock",
                           params={}, category="lock")],
    )
    report = agent.manager.run(plan)
    assert report.requires_confirmation, "门锁解锁应挂起待确认"
    assert report.pending_id, "应返回 pending_id"
    print("✓ 高危挂起：门锁 unlock → requires_confirmation + pending_id")


def test_hallucination_blocked():
    reg = DeviceRegistry()
    agent = HomeAgent(reg)
    r = agent.decide("你好呀")
    assert r["actions"] == [], f"寒暄不应执行动作，实际 {r['actions']}"
    print("✓ 幻觉拦截：寒暄「你好呀」→ 无任何设备动作")


def test_rule_fast_path_with_trace():
    reg = DeviceRegistry()
    agent = HomeAgent(reg)
    r = agent.decide("打开窗帘，空调调到24度")
    assert len(r["actions"]) >= 2, f"应至少两个动作，实际 {r['actions']}"
    assert r["decision_trace"], "应返回决策链路"
    steps = [t["step"] for t in r["decision_trace"]]
    assert "ground" in steps and "execute" in steps, f"trace 应含 ground+execute，实际 {steps}"
    print(f"✓ 规则快路径：actions={[(a['device_id'], a['command']) for a in r['actions']]} trace={steps}")


def test_invalid_action_dropped():
    reg = DeviceRegistry()
    agent = HomeAgent(reg)
    plan = TaskPlan(
        is_command=True, reply="test", state_version=agent.store.version, source="rule",
        sub_tasks=[SubTask(source="rule", device_id="no_such_device", command="turn_on",
                           params={}, category="light")],
    )
    report = agent.manager.run(plan)
    assert report.actions == [], "非法设备应被丢弃"
    assert any(t.ok is False for t in report.trace), "trace 应记录校验失败"
    print("✓ 校验拦截：不存在的设备被 verify 丢弃并记入 trace")


# ---------------------------------------------------------------------------
# v1.1 新增：证据链、参数包络、声明校验、单一执行入口
# ---------------------------------------------------------------------------
def test_readback_evidence():
    reg = DeviceRegistry()
    agent = HomeAgent(reg)
    r = agent.decide("打开客厅灯")
    result = r["results"][0]
    assert result["status"] == "confirmed", result
    rb = result["readback"]
    assert rb["confirmed"] and rb["trusted"] and rb["reason"] == "回读一致", rb
    entry = agent.store.get_audit(1)[0]
    assert entry["status"] == "confirmed" and entry.get("readback"), entry
    assert entry["version_after"] >= entry["version"], entry
    v = r["verification"]
    assert v["total"] == len(r["results"]) and v["confirmed"] >= 1, v
    print("✓ 每控必核：回读证据（原因/可信标记）+ 结局汇总写入审计，版本可还原")


def test_untrusted_readback_not_confirmed():
    reg = DeviceRegistry()
    dev = reg.get("light_living_01")
    dev.attributes["on"].value = "unknown"
    rb = reg._readback(dev, "turn_on", {}, {"settle_ms": 0, "tolerance": 0.5,
                                            "untrusted": {"unknown", ""}})
    assert rb["confirmed"] is False and rb["trusted"] is False, rb
    assert "不可信" in rb["reason"], rb
    print("✓ 回读：unknown 判为未核实，绝不把「没核实」当「符合预期」")


def test_param_envelope():
    reg = DeviceRegistry()
    agent = HomeAgent(reg)

    def plan(did, cmd, params, cat):
        return TaskPlan(is_command=True, reply="t", state_version=agent.store.version,
                        source="llm",
                        sub_tasks=[SubTask(source="llm", device_id=did, command=cmd,
                                           params=params, category=cat)])

    bad = agent.manager.run(plan("ac_living_01", "set_temperature", {"temperature": 99}, "ac"))
    assert bad.actions == [], bad.actions
    assert any("上限" in str(r.get("error", "")) for r in bad.results), bad.results

    bad2 = agent.manager.run(plan("light_living_01", "set_brightness", {"brightness": 500}, "light"))
    assert bad2.actions == [] and any("上限" in str(r.get("error", "")) for r in bad2.results)

    good = agent.manager.run(plan("ac_living_01", "set_temperature", {"temperature": 26}, "ac"))
    assert good.actions and good.results[0]["status"] == "confirmed", good.results
    print("✓ 参数包络：越界在 Manager 就被拒（不再依赖下游 clamp / 真实适配器）")


def test_claim_check():
    from backend.verification import check_claims
    reg = DeviceRegistry()
    meta = {d.device_id: {"name": d.name, "category": d.category, "room": d.room}
            for d in reg.all()}
    ok_row = {"ok": True, "device_id": "light_living_01", "command": "turn_on",
              "status": "confirmed", "readback": {"reason": "回读一致"}}
    acted = [{"device_id": "light_living_01", "command": "turn_on"}]

    truth = check_claims("已为您打开客厅灯。", acted, [ok_row], meta)
    assert truth.ok, truth.to_dict()

    lie = check_claims("已为您打开客厅灯。", [], [], meta)
    assert any(i.kind == "untouched_device" for i in lie.issues), lie.to_dict()

    opposite = check_claims("已关闭客厅灯。", acted, [ok_row], meta)
    assert any(i.kind == "command_mismatch" for i in opposite.issues), opposite.to_dict()

    delivered = dict(ok_row, status="delivered", readback={"reason": "回读不一致"})
    unverified = check_claims("已为您打开客厅灯。", acted, [delivered], meta)
    assert any(i.kind == "unverified" for i in unverified.issues), unverified.to_dict()

    fake_create = check_claims("已创建自动化规则：有人就开灯。", [], [], meta)
    assert any(i.kind == "creation_without_evidence" for i in fake_create.issues), fake_create.to_dict()
    print("✓ 声明校验：未动作 / 方向相反 / 未核实 / 虚构创建 四类都能识别")


def test_claim_check_wired():
    reg = DeviceRegistry()
    agent = HomeAgent(reg)
    orig_json, orig_avail = agent.llm.chat_json, agent.llm.available
    agent.llm.chat_json = lambda *a, **k: {"is_command": True,
                                           "reply": "已为您打开客厅灯。", "actions": []}
    agent.llm.available = True
    try:
        res = agent.decide("把整个屋子的氛围弄温馨一点")
    finally:
        agent.llm.chat_json, agent.llm.available = orig_json, orig_avail
    assert res["claim_issues"], res
    assert "提示" in res["reply"], res["reply"]
    assert any(t["step"] == "claim" for t in res["decision_trace"]), res["decision_trace"]
    print("✓ 声明校验已接入 decide：空动作却声称已执行 → 追加上如实注记 + trace 留痕")


def test_single_chokepoint():
    from backend.toolkit.guard import ManagerGuard
    reg = DeviceRegistry()
    agent = HomeAgent(reg)
    guard = ManagerGuard(agent.manager)
    before = agent.store.version
    res = guard.actuate("light_living_01", "turn_on", {})
    assert res.get("ok") and agent.store.version == before + 1, res
    entry = agent.store.get_audit(1)[0]
    assert entry["device_id"] == "light_living_01" and entry["source"] == "tool", entry
    blocked = guard.actuate("lock_entry_01", "unlock", {})
    assert blocked.get("requires_confirmation") is True, blocked
    print("✓ 单一执行入口：直调/场景/技能共用 ManagerGuard（审计与版本不漏）")


def test_no_direct_control_in_routes():
    """静态不变量：main.py 里不允许再出现直连 registry.control 的入口。"""
    src = Path(__file__).resolve().parent.joinpath("backend/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "control"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "registry"):
            hits.append(node.lineno)
    assert not hits, f"main.py 仍有直连 registry.control 的路径（行 {hits}）"
    print("✓ 单一执行入口：main.py 无直连 control（REST/WS/场景/技能全走护栏）")


def test_verification_layer_is_readonly():
    """静态不变量：输出层不得 import 设备层/租约层（与记忆层同款红线）。"""
    src = Path(__file__).resolve().parent.joinpath(
        "backend/verification/claim_checker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods.append(node.module or "")
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
    forbidden = [m for m in mods if "devices" in m or m.split(".")[-1] == "lease"]
    assert not forbidden, f"claim_checker 不应 import：{forbidden}"
    print("✓ 输出层只读：claim_checker 不 import 设备层/租约层")


def test_healthz_and_scope_endpoints():
    """存活探针与裁剪端点的契约（直接调协程，不启 HTTP 服务）。"""
    import asyncio
    from backend import main as m

    data = asyncio.run(m.healthz())
    assert data["ok"] and data["version"] == m.APP_VERSION, data
    assert set(data["checks"]) == {"data_dir_writable", "llm_configured", "scheduler"}, data
    # 无认证端点不得泄露设备/家庭信息
    payload = str(data)
    assert "light_living" not in payload and "客厅" not in payload, payload

    scope = asyncio.run(m.api_tools_scope("卧室太亮"))
    assert scope["devices"]["after"] == 4, scope["devices"]
    assert scope["tools"]["tools_after"] == 16, scope["tools"]
    assert "裁剪" in scope["summary"], scope["summary"]
    print("✓ /healthz 无认证且不泄露设备信息；/api/tools/scope 裁剪结果可复核")


if __name__ == "__main__":
    test_lease_validator()
    test_high_risk_suspend()
    test_hallucination_blocked()
    test_rule_fast_path_with_trace()
    test_invalid_action_dropped()
    test_readback_evidence()
    test_untrusted_readback_not_confirmed()
    test_param_envelope()
    test_claim_check()
    test_claim_check_wired()
    test_single_chokepoint()
    test_no_direct_control_in_routes()
    test_verification_layer_is_readonly()
    test_healthz_and_scope_endpoints()
    print("\n多智能体改造测试全部通过 ✅")
