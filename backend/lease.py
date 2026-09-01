# -*- coding: utf-8 -*-
"""授权租约（actuation lease）与校验。

每条改状态指令必须先由 Root 签发租约，Manager/Adapter 校验通过后才执行。
租约绑定：身份 + 设备范围 + 命令范围 + 状态版本 + 过期时间 + 风险等级。
对应《方案升级》中「授权租约 + 状态单一真相源」机制，是防 LLM 幻觉/越权/重放的核心护栏。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

# 高危操作策略：(category, command) 命中即需二次确认
HIGH_RISK = {
    ("lock", "unlock"),
    ("lock", "set_lock_mode"),
    ("gas_stove", "turn_on"),
    ("camera", "start_stream"),
}

# 高危操作中文描述（前端弹窗展示）
HIGH_RISK_DESC = {
    ("lock", "unlock"): "解锁门锁",
    ("lock", "set_lock_mode"): "切换门锁模式",
    ("gas_stove", "turn_on"): "打开燃气灶",
    ("camera", "start_stream"): "开启摄像头直播",
}


@dataclass
class Lease:
    """一条授权租约。"""
    lease_id: str
    agent: str                 # 签发方（root）
    source: str                # rule | llm
    device_id: str
    command: str
    params: dict = field(default_factory=dict)
    device_scope: list = field(default_factory=list)   # 允许设备白名单（[] = 任意）
    command_scope: list = field(default_factory=list)  # 允许命令白名单（[] = 任意）
    state_version: int = 0
    expires_at: float = 0.0
    risk: str = "low"          # low | high
    ttl: float = 10.0          # 租约有效期（秒）

    @classmethod
    def issue(cls, device_id: str, command: str, params: dict, source: str,
              state_version: int, category: str = "", ttl: float = 10.0) -> "Lease":
        risk = "high" if (category, command) in HIGH_RISK else "low"
        return cls(
            lease_id=uuid.uuid4().hex[:12],
            agent="root",
            source=source,
            device_id=device_id,
            command=command,
            params=params,
            device_scope=[device_id] if device_id else [],
            command_scope=[command] if command else [],
            state_version=state_version,
            expires_at=time.time() + ttl,
            risk=risk,
            ttl=ttl,
        )

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "agent": self.agent,
            "source": self.source,
            "device_id": self.device_id,
            "command": self.command,
            "params": self.params,
            "risk": self.risk,
            "state_version": self.state_version,
            "expires_at": round(self.expires_at, 2),
        }


class LeaseValidator:
    """租约校验器。"""

    @staticmethod
    def verify(lease: Lease, current_version: int, now: Optional[float] = None) -> tuple[bool, str]:
        """完整校验：过期 + 状态版本新鲜度 + 设备/命令范围。"""
        now = now or time.time()
        if now > lease.expires_at:
            return False, "租约已过期"
        if current_version != lease.state_version:
            return False, f"状态版本不匹配（租约签发于 v{lease.state_version}，当前 v{current_version}），指令已过期"
        if lease.device_scope and lease.device_id not in lease.device_scope:
            return False, f"设备 {lease.device_id} 不在授权范围 {lease.device_scope}"
        if lease.command_scope and lease.command not in lease.command_scope:
            return False, f"命令 {lease.command} 不在授权范围 {lease.command_scope}"
        return True, "ok"

    @staticmethod
    def is_expired(lease: Lease, now: Optional[float] = None) -> bool:
        """仅校验租约是否过期（正常执行路径用，同轮内状态版本会递增，故不逐动作校验版本）。"""
        return (now or time.time()) > lease.expires_at

    @staticmethod
    def is_high_risk(category: str, command: str) -> bool:
        return (category, command) in HIGH_RISK

    @staticmethod
    def high_risk_desc(category: str, command: str) -> str:
        return HIGH_RISK_DESC.get((category, command), f"{category}.{command}")
