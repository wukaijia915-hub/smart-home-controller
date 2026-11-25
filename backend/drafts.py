# -*- coding: utf-8 -*-
"""规则草稿箱（草稿 → 确认 两段式）。

## 为什么需要「草稿」这一层

用户说「如果客厅没人了就把灯关掉」。旧实现是**诚实拒绝**：这不是立即动作，
系统没有从对话建规则的能力，所以既不执行也不假装。诚实是对的，但产品上
最好的答案是第三种：**生成草稿，让用户看见"到底会创建什么"，确认后才落库**。

对照项目 Aether 把同一套机制用在"从对话建自动化规则"上
（`app/services/pending_rules.py`），三个细节值得照搬：

1. **草稿不持久化**：TTL 10 分钟、进程重启即丢。草稿是短命的交互中间态，
   持久化只会制造"幽灵草稿"（用户三天后确认了一条早忘了的规则）。
2. **草稿按会话存**：确认接口要校验归属，不能拿到 id 就能替别人确认。
3. **不变式守在共享层**：确认逻辑不写在 REST 路由里，而是收在
   `DraftStore.confirm` + 端点共用的一个函数里 —— 否则语音/WS 通道会绕过去。

## 与高危确认的区别（别混为一谈）

- `StateStore.pending`：**动作级**二次确认，10 秒租约 TTL，确认即执行一次。
- `DraftStore`：**结构级**二次确认，10 分钟 TTL，确认即写入一条自动化规则。
  规则此后在无人确认的路径上执行，所以高危动作必须在确认时显式允许
  （`allow_high_risk`），默认拒绝。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from .config_loader import load_draft_rules
from .lease import LeaseValidator


@dataclass
class Draft:
    draft_id: str
    kind: str
    payload: dict
    summary: str
    session: str = "default"
    created_at: float = 0.0
    expires_at: float = 0.0
    status: str = "pending"          # pending | confirmed | cancelled | expired

    def to_dict(self) -> dict:
        return {
            "draft_id": self.draft_id,
            "kind": self.kind,
            "payload": self.payload,
            "summary": self.summary,
            "session": self.session,
            "status": self.status,
            "created_at": round(self.created_at, 3),
            "expires_at": round(self.expires_at, 3),
            "ttl_seconds": max(0, round(self.expires_at - time.time(), 1)),
        }


class DraftStore:
    """内存草稿箱（TTL + 容量上限 + 线程安全）。"""

    def __init__(self, ttl_seconds: Optional[float] = None,
                 max_pending: Optional[int] = None):
        cfg = {}
        try:
            cfg = load_draft_rules() or {}
        except Exception:
            cfg = {}
        self.ttl = float(ttl_seconds if ttl_seconds is not None
                         else cfg.get("ttl_seconds", 600) or 600)
        self.max_pending = int(max_pending if max_pending is not None
                               else cfg.get("max_pending", 20) or 20)
        self._items: dict[str, Draft] = {}
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def create(self, payload: dict, summary: str, session: str = "default",
               kind: str = "event_route") -> Optional[Draft]:
        """新建草稿。超出容量上限时先清理过期项，仍满则返回 None（不排队）。"""
        with self._lock:
            self._purge_locked()
            if len(self._items) >= self.max_pending:
                return None
            now = time.time()
            draft = Draft(
                draft_id=uuid.uuid4().hex[:10],
                kind=kind,
                payload=payload,
                summary=summary,
                session=session,
                created_at=now,
                expires_at=now + self.ttl,
            )
            self._items[draft.draft_id] = draft
            return draft

    def get(self, draft_id: str) -> Optional[Draft]:
        with self._lock:
            self._purge_locked()
            return self._items.get(draft_id)

    def list(self, session: Optional[str] = None) -> list[dict]:
        with self._lock:
            self._purge_locked()
            items = [d for d in self._items.values()
                     if session is None or d.session == session]
            return [d.to_dict() for d in sorted(items, key=lambda d: d.created_at, reverse=True)]

    def confirm(self, draft_id: str) -> Optional[Draft]:
        """取出并标记为已确认。返回 None 表示不存在/已过期。"""
        with self._lock:
            self._purge_locked()
            draft = self._items.pop(draft_id, None)
            if draft is None:
                return None
            draft.status = "confirmed"
            return draft

    def cancel(self, draft_id: str) -> bool:
        with self._lock:
            self._purge_locked()
            draft = self._items.pop(draft_id, None)
            if draft is None:
                return False
            draft.status = "cancelled"
            return True

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items.clear()
            return n

    def stats(self) -> dict:
        with self._lock:
            self._purge_locked()
            return {"pending": len(self._items), "ttl_seconds": self.ttl,
                    "max_pending": self.max_pending}

    def reload_config(self) -> dict:
        """配置热重载：重新读 TTL / 容量上限（存量草稿的过期时间不变）。"""
        cfg = {}
        try:
            cfg = load_draft_rules() or {}
        except Exception:
            cfg = {}
        with self._lock:
            self.ttl = float(cfg.get("ttl_seconds", self.ttl) or self.ttl)
            self.max_pending = int(cfg.get("max_pending", self.max_pending) or self.max_pending)
            return {"ttl_seconds": self.ttl, "max_pending": self.max_pending}

    # ---------- 内部 ----------
    def _purge_locked(self) -> int:
        now = time.time()
        expired = [k for k, d in self._items.items() if d.expires_at <= now]
        for k in expired:
            self._items[k].status = "expired"
            del self._items[k]
        return len(expired)

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_locked()


# 全局单例（草稿短命，不需要落盘）
draft_store = DraftStore()


# ---------------------------------------------------------------------------
# 确认逻辑（业务层，不属于任何路由）
#
# 放在这里而不是 main.py 的路由里，是 Aether 那条教训的直接应用：
# **不变式守在共享层**。只在 REST 路由校验的话，WS / 语音通道能绕过同一条规则。
# 于是 REST 与 WS 都调用本函数，规则只有一份。
# ---------------------------------------------------------------------------
def draft_high_risk(actions: list, registry) -> list[str]:
    """草稿里哪些动作属于高危（按**当前**设备类别判定，不信任草稿里的旧结论）。"""
    high = []
    for a in actions or []:
        device = registry.get(a.get("device_id", ""))
        if device is None:
            continue
        command = a.get("command", "")
        if LeaseValidator.is_high_risk(device.category, command):
            high.append(f"{device.name}.{command}")
    return high


def confirm_draft(store: DraftStore, router, registry, draft_id: str,
                  allow_high_risk: bool = False) -> tuple[bool, dict]:
    """把草稿写成一条自定义事件路由。

    返回 (是否成功, 载荷)。失败时载荷含 `code`，便于调用方映射 HTTP 状态。
    """
    draft = store.get(draft_id)
    if draft is None:
        return False, {"code": "draft_not_found",
                       "error": "草稿不存在或已过期（草稿默认 10 分钟有效）"}
    if draft.kind != "event_route":
        return False, {"code": "unsupported_draft", "error": f"不支持的草稿类型: {draft.kind}"}

    payload = dict(draft.payload or {})
    actions = [dict(a) for a in (payload.get("actions") or [])]
    if not actions:
        return False, {"code": "empty_actions", "error": "草稿里没有任何设备动作"}

    high = draft_high_risk(actions, registry)
    if high and not allow_high_risk:
        return False, {
            "code": "high_risk_requires_allow",
            "error": ("该规则包含高危动作：" + "、".join(high) +
                      "。无人值守的自动路径默认不得执行高危动作；确需自动执行，"
                      "请在确认请求里显式传 allow_high_risk=true。"),
            "high_risk": high,
        }
    for a in actions:
        device = registry.get(a.get("device_id", ""))
        if device and LeaseValidator.is_high_risk(device.category, a.get("command", "")):
            # 让这个决定被写下来、被评审看到，而不是悄悄发生
            a["allow_high_risk"] = True

    route = {
        "id": f"user_{draft_id}",
        "desc": payload.get("desc") or draft.summary,
        "cooldown_seconds": int(payload.get("cooldown_seconds", 300) or 300),
        "match": dict(payload.get("match") or {}),
        "actions": actions,
        "created_from": "draft",
        "source_text": payload.get("source_text", ""),
    }
    route = router.add_route(route)
    store.confirm(draft_id)
    return True, {
        "route": route,
        "reply": (f"已创建联动规则「{route['id']}」：{str(route['desc']).rstrip('。')}。"
                  f"此后匹配事件触发时会自动执行，每条动作仍带回读证据与审计。"),
    }
