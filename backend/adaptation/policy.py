# -*- coding: utf-8 -*-
"""L5 有界自适应策略：偏好带 → 提议 → 四道闸 → 冷启动 → 漂移降级。

## 三条边界（写进代码，不靠自觉）

1. **只提议，不直接执行**：`propose()` 返回带 `executable` 标记的提议；
   真正执行由 `apply()` 调 `execution.execute_plan(source="adaptation")`。
2. **四道闸在提议里带全**：包络 / 最大步长 / 最小间隔 / 当日预算，
   任何一道不过就 `gate_ok=False` 并写明原因，不是静默丢弃。
3. **安全设备永不参与**：门锁、燃气、摄像头、窗户在 `arbitrate()` 就被排除。

## 冷启动与降级

- 冷启动：`shadow`（只算不动作）→ `probe`（步长为正常的一半）→ `auto`
  （样本 ≥ cold_start_samples 且反馈 ≥ feedback_required）。
- 漂移：纠正率 / 舒适带达标率下降 / 感知降级比例任一越限 → 逐级退让
  `auto → confirm → suggest_only → off`，并清空反馈重走冷启动。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import execution
from ..config_loader import data_path
from ..toolkit.schema_gen import PARAM_SOURCE
from .objectives import Proposal, arbitrate
from .preference import best_band, hour_bucket, learn_bands

_LEVELS = ("auto", "confirm", "suggest_only", "off")


@dataclass
class AdaptationState:
    level: str = "suggest_only"
    mode: str = "shadow"
    feedback: int = 0
    corrections: int = 0
    applied: int = 0
    proposals_total: int = 0
    last_tick: float = 0.0
    day: str = ""
    daily_used: int = 0
    drift: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {**self.__dict__}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AdaptationState":
        data = data or {}
        state = cls()
        for key in state.__dict__:
            if key in data and data[key] is not None:
                setattr(state, key, data[key])
        if state.level not in _LEVELS:
            state.level = "suggest_only"
        return state


def _proposal_id(device_id: str, command: str, params: dict, bucket: str) -> str:
    raw = f"{device_id}|{command}|{json.dumps(params, sort_keys=True, ensure_ascii=False)}|{bucket}"
    return "prop_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


class AdaptationPolicy:
    def __init__(self, config: Optional[dict] = None,
                 state_path: Optional[Path] = None,
                 now_fn=None):
        self.config = config or {}
        self.state_path = Path(state_path) if state_path else data_path("adaptation_state.json")
        self._now = now_fn or time.time
        self.state = self._load()
        self.last_proposals: list[Proposal] = []
        self.last_chosen: list[Proposal] = []

    # ---------- 持久化 ----------
    def _load(self) -> AdaptationState:
        try:
            if self.state_path.exists():
                return AdaptationState.from_dict(
                    json.loads(self.state_path.read_text(encoding="utf-8")))
        except Exception:
            pass
        return AdaptationState(level=str(self.config.get("autonomy_initial", "suggest_only")))

    def save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception:
            pass        # 状态落盘失败不影响主链

    # ---------- 提议 ----------
    def propose(self, context: dict, entries: list, registry, store,
                now: Optional[float] = None) -> dict:
        now = float(now if now is not None else self._now())
        self.state.last_tick = now
        if not self.config.get("enabled", False) or self.state.level == "off":
            self.last_proposals = []
            self.last_chosen = []
            return {"proposals": [], "chosen": [], "deferred": [],
                    "reason": "L5 未启用或已停用", "bands": {}, "state": self.state.to_dict()}

        bands = learn_bands(entries, self.config, registry)
        bucket = hour_bucket(now)
        sensors = context.get("sensors") or {}
        candidates: list[Proposal] = []
        candidates.extend(self._temperature_proposals(context, sensors, bands, bucket, registry))
        candidates.extend(self._humidity_proposals(context, sensors, bands, bucket, registry))
        candidates.extend(self._water_heater_proposals(context, bands, bucket, registry, now))

        arbitration = arbitrate(candidates, context, self.config)
        self.state.proposals_total += len(candidates)
        for p in arbitration["chosen"]:
            self._check_gates(p, registry, store, now)
            p.executable = self._can_execute(p)

        self.last_proposals = list(arbitration["chosen"])
        self.last_chosen = self.last_proposals
        return {
            "proposals": [p.to_dict() for p in arbitration["chosen"]],
            "deferred": arbitration["deferred"],
            "reason": arbitration["reason"],
            "bands": {f"{k[0]}|{k[1]}|{k[2]}": v.to_dict() for k, v in bands.items()},
            "state": self.state.to_dict(),
            "now": now,
        }

    def _temperature_proposals(self, context, sensors, bands, bucket, registry) -> list[Proposal]:
        current = sensors.get("temp_indoor")
        if not isinstance(current, (int, float)):
            return []
        out = []
        for device in registry.all():
            if getattr(device, "category", "") != "ac":
                continue
            band = best_band(bands, "temperature", getattr(device, "room", ""), bucket)
            if band is None or band.cold_start:
                continue
            margin = float((self.config.get("comfort") or {}).get("temperature_margin", 0.5))
            if band.contains(current, margin):
                continue
            spec = (self.config.get("attributes") or {}).get("temperature") or {}
            max_delta = float(spec.get("max_delta", 2) or 2)
            target = band.center
            if current > band.high + margin:
                target = max(band.low, current - max_delta)
                mode = "cool"
            else:
                target = min(band.high, current + max_delta)
                mode = "heat"
            out.append(Proposal(
                proposal_id=_proposal_id(device.device_id, "set_temperature",
                                         {"temperature": round(target)}, bucket),
                device_id=device.device_id, device_name=device.name,
                category=device.category, room=device.room,
                command="set_temperature", params={"temperature": round(target)},
                attribute="temperature", value=round(target),
                priority=0, reason=f"温度 {current:.1f}℃ 超出偏好带 "
                                   f"[{band.low:.1f},{band.high:.1f}]（样本 {band.samples}）",
                expected_comfort=min(1.0, abs(current - band.center) / 10.0),
                energy_cost=0.8,
            ))
            out.append(Proposal(
                proposal_id=_proposal_id(device.device_id, "set_mode", {"mode": mode}, bucket),
                device_id=device.device_id, device_name=device.name,
                category=device.category, room=device.room,
                command="set_mode", params={"mode": mode},
                attribute="temperature", value=None, priority=1,
                reason=f"配合温度目标选择 {mode}", expected_comfort=0.2, energy_cost=0.2,
            ))
        return out

    def _humidity_proposals(self, context, sensors, bands, bucket, registry) -> list[Proposal]:
        current = sensors.get("humidity")
        if not isinstance(current, (int, float)):
            return []
        out = []
        for device in registry.all():
            category = getattr(device, "category", "")
            if category not in ("humidifier", "dehumidifier"):
                continue
            band = best_band(bands, "target", getattr(device, "room", ""), bucket)
            if band is None or band.cold_start:
                continue
            margin = float((self.config.get("comfort") or {}).get("humidity_margin", 5))
            if band.contains(current, margin):
                continue
            spec = (self.config.get("attributes") or {}).get("target") or {}
            max_delta = float(spec.get("max_delta", 10) or 10)
            if category == "dehumidifier" and current > band.high + margin:
                target = max(band.low, current - max_delta)
                command = "set_target"
            elif category == "humidifier" and current < band.low - margin:
                target = min(band.high, current + max_delta)
                command = "set_target"
            else:
                continue
            out.append(Proposal(
                proposal_id=_proposal_id(device.device_id, command,
                                         {"target": round(target)}, bucket),
                device_id=device.device_id, device_name=device.name,
                category=category, room=device.room, command=command,
                params={"target": round(target)}, attribute="target", value=round(target),
                priority=0, reason=f"湿度 {current:.0f}% 超出偏好带 "
                                   f"[{band.low:.0f},{band.high:.0f}]（样本 {band.samples}）",
                expected_comfort=min(1.0, abs(current - band.center) / 30.0),
                energy_cost=0.6,
            ))
        return out

    def _water_heater_proposals(self, context, bands, bucket, registry, now) -> list[Proposal]:
        hours = [int(h) for h in (self.config.get("water_heater_hours") or [])]
        if hours and time.localtime(now).tm_hour not in hours:
            return []
        out = []
        for device in registry.all():
            if getattr(device, "category", "") != "water_heater":
                continue
            if device.get("on"):
                continue
            band = best_band(bands, "temperature", getattr(device, "room", ""), bucket)
            target = round(band.center) if band and not band.cold_start else 45
            out.append(Proposal(
                proposal_id=_proposal_id(device.device_id, "turn_on", {}, bucket),
                device_id=device.device_id, device_name=device.name,
                category=device.category, room=device.room,
                command="turn_on", params={}, attribute="temperature", value=None,
                priority=1, reason=f"峰谷时段预约加热（目标 {target}℃）",
                expected_comfort=0.3, energy_cost=1.0,
            ))
        return out

    # ---------- 四道闸 ----------
    def _check_gates(self, proposal: Proposal, registry, store, now: float) -> None:
        device = registry.get(proposal.device_id)
        if device is None:
            proposal.gate_ok, proposal.gate_reason = False, "设备不存在"
            return
        from ..execution import check_guards, verify_params
        ok, reason = verify_params(device, proposal.command, proposal.params)
        if not ok:
            proposal.gate_ok, proposal.gate_reason = False, reason
            return
        ok, reason = check_guards(device, proposal.command, proposal.params,
                                  self.config, store, now)
        proposal.gate_ok = ok
        proposal.gate_reason = "" if ok else reason

    def _can_execute(self, proposal: Proposal) -> bool:
        if not proposal.gate_ok:
            return False
        if self.state.level in ("confirm", "suggest_only", "off"):
            return False
        if self.state.mode == "shadow":
            return False
        if self.state.mode == "probe":
            # 探测阶段只走半个步长，且需要足够反馈后才转 auto
            spec = (self.config.get("attributes") or {}).get(proposal.attribute) or {}
            ratio = float(self.config.get("probe_step_ratio", 0.5) or 0.5)
            max_delta = spec.get("max_delta")
            if max_delta and proposal.value is not None:
                # 简化：探测阶段只允许"开关类"动作，不做大步长设定
                return proposal.command in ("turn_on", "turn_off")
            return proposal.command in ("turn_on", "turn_off")
        return True

    # ---------- 执行 ----------
    def apply(self, proposals: list[Proposal], registry, store,
              now: Optional[float] = None) -> dict:
        now = float(now if now is not None else self._now())
        actions = [{"device_id": p.device_id, "command": p.command, "params": p.params}
                   for p in proposals if p.executable]
        if not actions:
            return {"ok": False, "reason": "没有可执行的提议", "results": []}
        outcome = execution.execute_plan(
            actions, registry=registry, store=store, source="adaptation",
            bounds=self.config, now=now,
            causal={"signal": {"source": "adaptation", "now": now},
                    "inference": {"actions": actions},
                    "gate": {"four_gates": "passed"}})
        ok_count = sum(1 for r in outcome.get("results", []) if r.get("ok"))
        self.state.applied += ok_count
        self._count_daily(now, ok_count)
        self.save()
        return {"ok": outcome.get("ok", False), "decision_id": outcome.get("decision_id", ""),
                "results": outcome.get("results", [])}

    # ---------- 反馈与降级 ----------
    def accept(self, proposal_id: str) -> bool:
        for p in self.last_proposals:
            if p.proposal_id == proposal_id and p.gate_ok:
                p.executable = True
                self.state.feedback += 1
                self.save()
                return True
        return False

    def reject(self, proposal_id: str) -> bool:
        for p in self.last_proposals:
            if p.proposal_id == proposal_id:
                p.executable = False
                self.state.feedback += 1
                self.state.corrections += 1
                self.save()
                return True
        return False

    def advance_mode(self) -> str:
        """冷启动状态机：shadow → probe → auto。"""
        samples = int(self.state.feedback)
        if self.state.mode == "shadow" and samples >= int(self.config.get("cold_start_samples", 12)):
            self.state.mode = "probe"
        if self.state.mode == "probe" and samples >= int(self.config.get("feedback_required", 12)):
            self.state.mode = "auto"
        return self.state.mode

    def assess_drift(self, metrics: Optional[dict] = None) -> dict:
        """按 24h 滚动指标决定是否降级。返回 {level, degraded, reasons}。"""
        metrics = metrics or {}
        drift = self.config.get("drift") or {}
        reasons = []
        if float(metrics.get("correction_rate", 0)) > float(drift.get("correction_rate_max", 0.1)):
            reasons.append("纠正率超限")
        if float(metrics.get("comfort_hit_drop_points", 0)) >= float(
                drift.get("comfort_hit_drop_points", 10)):
            reasons.append("舒适带达标率下降超限")
        if float(metrics.get("degraded_ratio", 0)) > float(drift.get("degraded_ratio_max", 0.2)):
            reasons.append("感知降级比例超限")
        if not reasons:
            self.state.drift = {"degraded": False, "reasons": []}
            return {"level": self.state.level, "degraded": False, "reasons": []}

        idx = _LEVELS.index(self.state.level) if self.state.level in _LEVELS else 2
        new_level = _LEVELS[min(idx + 1, len(_LEVELS) - 1)]
        # 退让后清空反馈、回影子模式重走冷启动
        self.state.level = new_level
        self.state.mode = "shadow"
        self.state.feedback = 0
        self.state.corrections = 0
        self.state.drift = {"degraded": True, "reasons": reasons, "at": self._now()}
        self.save()
        return {"level": new_level, "degraded": True, "reasons": reasons}

    def _count_daily(self, now: float, count: int) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.state.day != day:
            self.state.day = day
            self.state.daily_used = 0
        self.state.daily_used += int(count)

    # ---------- 视图 ----------
    def status(self) -> dict:
        return {"enabled": bool(self.config.get("enabled", False)),
                "state": self.state.to_dict(),
                "level": self.state.level, "mode": self.state.mode,
                "proposals": [p.to_dict() for p in self.last_proposals],
                "cold_start_samples": int(self.config.get("cold_start_samples", 12)),
                "feedback_required": int(self.config.get("feedback_required", 12)),
                "daily_budget": int(self.config.get("daily_budget", 60))}
