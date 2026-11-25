# -*- coding: utf-8 -*-
"""记忆源三：经验复用（成功规划案例回灌 prompt）。

闭环：慢环规划 → 执行 → 记录结果与评分 → 下次同类请求时把高分案例注入 prompt。

**与心镜 `evolution/memory.py` 的两处刻意不同**

1. **去掉 `random.sample`**。心镜检索时对候选做随机抽样，同一 category 每次注入
   不同的案例 —— 这让「自进化」的效果**无法被验证**：输入相同、注入不同，
   任何 A/B 对比都不成立。这里改为**确定性排序** `rating * 2 + 关键词重合度`，
   同样的请求永远注入同样的案例，因此可以真的测「注入 vs 不注入」的差异。
2. **字段去领域化**。心镜的 `student_name` 是心理领域字段，照搬会在答辩时被问
   「你的智能家居项目里为什么有 student_name」。这里用 `context_key`（如
   `客厅|空调`），category 也从 `daily_report/...` 改为 `scene_plan / fuzzy_command
   / active_alert`。

**诚实边界（必须讲清楚，否则就是夸大）**

- 这叫「**基于成功案例的 prompt 级经验复用**」，**不叫「自进化」/「自学习」**。
  没有梯度更新、没有策略迭代，只是把历史成功案例放进 prompt。
- `rating` 默认**由执行结果推导**（全部动作执行成功=5、部分=3、全失败=1），
  它衡量的是「这轮规划**执行**成功了吗」，**不是「用户满意吗」**。
  用户满意度只能靠 `POST /api/memory/feedback` 的显式反馈拿，
  `rating_source` 字段如实区分这两种来源（`auto_execution` / `user`）。

存储：`data/experience.jsonl`（append-only，一行一条）。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Optional

from ..config_loader import data_path
from .manuals import keyword_score

EXPERIENCE_PATH = data_path("experience.jsonl")

# category 的取值域。**只列真实存在写入方的类别** —— 声明了却没有写入方的
# 类别是死配置，会在答辩时被问「这个 scene_plan 是什么」。将来要加，
# 等它真的有调用方再加。
#   fuzzy_command  慢环对模糊指令的规划（root_agent._llm_plan 之后由 agent.decide 记录）
#   active_alert   事件路由触发的环境联动（main.py 的告警循环记录）
CATEGORIES = ("fuzzy_command", "active_alert")

MAX_SCAN_LINES = 2000       # 单次检索最多回看的行数，避免文件无限增长后拖慢请求
DEFAULT_MIN_RATING = 4


def derive_rating(ok_count: int, total: int) -> int:
    """由执行结果推导评分。total=0 视为不可评分（返回 0，不参与检索）。

    这是**执行成功率**的粗分档，不是满意度。分档故意粗（3 档），
    因为再细的分档会让人误以为它测的是别的东西。
    """
    if total <= 0:
        return 0
    if ok_count >= total:
        return 5
    if ok_count == 0:
        return 1
    return 3


class ExperienceStore:
    def __init__(self, path: Path | None = None):
        self.path = path or EXPERIENCE_PATH
        self._lock = threading.Lock()
        self.last_injected: list[dict] = []     # 最近一次注入的案例（前端可查）

    # ---------- 写入 ----------
    def record(self, category: str, context_key: str, request: str,
               actions: list, reply: str = "", rating: Optional[int] = None,
               rating_source: str = "auto_execution", feedback: str = "",
               source: str = "") -> Optional[str]:
        """记录一条经验。rating 为 None 时按 total=0 处理（不可评分，不参与检索）。"""
        if category not in CATEGORIES:
            category = "fuzzy_command"
        entry = {
            "id": uuid.uuid4().hex[:8],
            "ts": time.time(),
            "category": category,
            "context_key": context_key or "",
            "request": (request or "")[:120],
            "actions": [
                {"device_id": a.get("device_id", ""), "command": a.get("command", "")}
                for a in (actions or []) if isinstance(a, dict)
            ][:6],
            "reply": (reply or "")[:200],
            "rating": rating if rating is not None else 0,
            "rating_source": rating_source,
            "feedback": (feedback or "")[:200],
            "plan_source": source,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry["id"]
        except Exception as e:
            print(f"[记忆·经验] 写入失败: {e}")
            return None

    def rate(self, entry_id: str, rating: int, feedback: str = "") -> bool:
        """用户显式评分。JSONL 是 append-only，这里追加一条同 id 的覆盖记录。

        覆盖而不是原地改：append-only 的文件在崩溃/并发下不会损坏，
        且「用户先给 5 分后改成 3 分」这件事本身是有价值的审计信息。
        """
        if not entry_id or not (1 <= int(rating) <= 5):
            return False
        patch = {"id": entry_id, "rating": int(rating), "rating_source": "user",
                 "feedback": (feedback or "")[:200], "ts": time.time(), "_patch": True}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(patch, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            print(f"[记忆·经验] 评分写入失败: {e}")
            return False

    # ---------- 读取 ----------
    def _load_entries(self) -> list[dict]:
        """读出全部经验，把 `_patch` 覆盖记录合并进原条目。"""
        if not self.path.exists():
            return []
        by_id: dict[str, dict] = {}
        order: list[str] = []
        try:
            with self._lock:
                lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        for line in lines[-MAX_SCAN_LINES:]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = entry.get("id")
            if not eid:
                continue
            if entry.get("_patch"):
                if eid in by_id:
                    by_id[eid].update({k: v for k, v in entry.items() if k != "_patch"})
                continue
            by_id[eid] = entry
            order.append(eid)
        return [by_id[i] for i in order]

    @staticmethod
    def rank(entries: list[dict], query: str = "", prefer_category: str = "") -> list[dict]:
        """确定性排序：`rating * 2 + 关键词重合度 [+ 同类 +1]`，同分按时间倒序。

        这个函数就是「去掉 random.sample」的落点 —— 它是纯函数，
        同样输入永远同样输出，因此经验复用的效果可被单测和 A/B 验证。
        """
        scored = []
        for e in entries:
            score = float(e.get("rating", 0)) * 2.0
            if query:
                haystack = f"{e.get('context_key', '')} {e.get('request', '')} " \
                           f"{' '.join(a.get('device_id', '') + a.get('command', '') for a in e.get('actions', []))}"
                score += keyword_score(haystack, query)
            if prefer_category and e.get("category") == prefer_category:
                score += 1.0        # 加权而非过滤：同类优先，但不排斥其他类的可用案例
            scored.append((score, float(e.get("ts", 0)), e))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [e for _, _, e in scored]

    def retrieve(self, prefer_category: str = "", query: str = "", limit: int = 2,
                 min_rating: int = DEFAULT_MIN_RATING) -> list[dict]:
        """检索可注入 prompt 的高质量案例。"""
        candidates = [e for e in self._load_entries()
                      if float(e.get("rating", 0)) >= min_rating]
        return self.rank(candidates, query=query, prefer_category=prefer_category)[:limit]

    def build_context(self, prefer_category: str = "", query: str = "", limit: int = 2) -> str:
        """构造注入慢环 prompt 的经验段落。无案例时返回空串（不编造）。"""
        cases = self.retrieve(prefer_category=prefer_category, query=query, limit=limit)
        self.last_injected = [
            {"id": c.get("id"), "context_key": c.get("context_key"),
             "rating": c.get("rating"), "rating_source": c.get("rating_source"),
             "request": c.get("request")}
            for c in cases
        ]
        if not cases:
            return ""
        lines = ["【历史成功案例参考】（同类请求此前执行成功的规划，仅供参考）"]
        for i, c in enumerate(cases, 1):
            acts = "、".join(f"{a.get('device_id')}.{a.get('command')}"
                            for a in c.get("actions", []))
            src = "用户评分" if c.get("rating_source") == "user" else "执行结果"
            lines.append(f"{i}. 请求「{c.get('request', '')}」→ 动作：{acts or '（无）'} "
                         f"（{src} {c.get('rating', '?')}/5）")
        return "\n".join(lines)

    # ---------- 统计 ----------
    def stats(self) -> dict:
        entries = self._load_entries()
        rated = [e for e in entries if float(e.get("rating", 0)) > 0]
        ratings = [float(e["rating"]) for e in rated]
        top = Counter(e.get("category", "other") for e in entries)
        recent = sorted(entries, key=lambda e: e.get("ts", 0), reverse=True)[:5]
        return {
            "total": len(entries),
            "rated": len(rated),
            "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
            "rating_trend": self._trend(ratings),
            "user_rated": sum(1 for e in entries if e.get("rating_source") == "user"),
            "by_category": dict(top),
            "recent": [
                {"id": e.get("id"), "category": e.get("category"),
                 "context_key": e.get("context_key"), "rating": e.get("rating"),
                 "rating_source": e.get("rating_source"), "request": e.get("request")}
                for e in recent
            ],
            "last_injected": self.last_injected,
            "note": "rating 默认由执行结果推导（衡量执行是否成功），显式用户评分以 rating_source=user 区分",
        }

    @staticmethod
    def _trend(ratings: list[float]) -> str:
        """最近 5 次 vs 再前 5 次评分的走向。样本不足 10 条时如实说数据不足。"""
        if len(ratings) < 10:
            return "数据不足"
        recent = sum(ratings[-5:]) / 5
        older = sum(ratings[-10:-5]) / 5
        diff = recent - older
        if diff > 0.3:
            return "上升"
        if diff < -0.3:
            return "下降"
        return "持平"


experience = ExperienceStore()
