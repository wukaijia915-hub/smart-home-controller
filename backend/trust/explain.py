# -*- coding: utf-8 -*-
"""决策解释：六段链重建 + 失败归因。

数据只来自 `StateStore`（审计与决策链），**不读设备、不执行动作**。
审计是环形缓冲：滚出后必须如实返回 None + 原因，不能编造一条"看起来完整"的链。
"""
from __future__ import annotations

from typing import Optional


def attribute_failure(record: dict) -> str:
    """把一条审计记录归因到具体层。

    层名与申报书一致：sensor / inference / gate / lease / device / readback。
    """
    layer = str(record.get("failed_layer") or "")
    if layer:
        return layer
    status = record.get("status")
    if status == "delivered":
        return "readback"
    if not record.get("ok"):
        return "device"
    return ""


def explain(store, decision_id: str,
            audit_limit: Optional[int] = None) -> dict:
    """返回一条决策的完整链条；滚出缓冲时返回 ok=False + 原因。"""
    if store is None:
        return {"ok": False, "decision_id": decision_id, "reason": "审计存储不可用"}
    chain = store.get_decision(decision_id)
    if chain is None:
        return {
            "ok": False,
            "decision_id": decision_id,
            "reason": (f"该决策已滚出环形缓冲（最多保留 {getattr(store, 'max_decisions', '?')} 条"
                       f"决策，审计最多 {getattr(store, 'max_log', '?')} 条）"),
        }
    limit = int(audit_limit or getattr(store, "max_log", 2000))
    actions = [e for e in store.get_audit(limit=limit)
               if e.get("decision_id") == decision_id]
    failed = chain.get("failed_layer") or ""
    if not failed:
        failed = next((attribute_failure(a) for a in actions if attribute_failure(a)), "")
    return {
        "ok": True,
        "decision_id": decision_id,
        "source": chain.get("source", ""),
        "created_at": chain.get("created_at", 0),
        "failed_layer": failed,
        "stages": chain.get("stages", []),
        "actions": actions,
        "note": ("六段链按 signal → inference → gate → lease → action → readback 排列；"
                 "缺段表示该路径没有对应环节（不会伪造空段）"),
    }


def explain_recent(store, limit: int = 20) -> list[dict]:
    """最近决策的摘要（给 /api/trust/recent 与前端列表）。"""
    if store is None:
        return []
    out = []
    for chain in store.recent_decisions(limit=limit):
        out.append({
            "decision_id": chain.get("decision_id", ""),
            "source": chain.get("source", ""),
            "created_at": chain.get("created_at", 0),
            "failed_layer": chain.get("failed_layer", ""),
            "stage_count": len(chain.get("stages", [])),
        })
    return out
