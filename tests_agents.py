# -*- coding: utf-8 -*-
"""多智能体改造验证：授权租约 / 幻觉拦截 / 高危二次确认 / 非法动作校验。

不依赖真实硬件，也不依赖 LLM 稳定性——高危挂起与非法拦截用 rule 子任务直达 Manager 校验层。
"""
import sys
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


if __name__ == "__main__":
    test_lease_validator()
    test_high_risk_suspend()
    test_hallucination_blocked()
    test_rule_fast_path_with_trace()
    test_invalid_action_dropped()
    print("\n多智能体改造测试全部通过 ✅")
