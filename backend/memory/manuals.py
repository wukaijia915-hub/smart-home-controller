# -*- coding: utf-8 -*-
"""记忆源一：设备手册检索。

按用户指令关键词检索设备知识，注入慢环 prompt，让模型知道「这个设备怎么用、
有什么节能窍门」，而不是只看到一份设备清单。零依赖关键词打分（无向量库、
无嵌入模型）—— 17 台设备的手册用倒排索引是杀鸡用牛刀。

`keyword_score()` 是**共用**的：经验复用（`experience.py`）也用它做「当前请求
vs 历史案例」的相关度排序。两处共用一个函数是为了避免"手册检索用一套相关度、
经验检索用另一套"这种答辩时解释不清的不一致。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 静态知识资产：随仓库提交，不进 data/（data/ 是运行时状态）
MANUALS_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "device_manuals.json"

_CJK = re.compile(r"[一-龥]{2,}")


def keyword_score(text: str, query: str) -> float:
    """通用相关度打分：query 与 text 的关键词重合度。

    只用中文词块（2 字以上）匹配，不做分词 —— 家居指令的领域词
    （"空调"/"亮度"/"窗帘"）基本都是 2-4 字，词块匹配足够，
    且不会因分词器差异产生不可复现的排序。
    """
    if not text or not query:
        return 0.0
    query_lower = query.lower()
    text_lower = text.lower()
    score = 0.0
    for word in _CJK.findall(query_lower):
        if word in text_lower:
            score += 1.0
    # 英文/数字 token 单独计一次，避免「AC」这类词被中文规则漏掉
    for token in re.findall(r"[a-z0-9]{2,}", query_lower):
        if token in text_lower:
            score += 0.5
    return score


class ManualStore:
    def __init__(self, path: Path | None = None):
        self.path = path or MANUALS_PATH
        self.manuals: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data.get("manuals", []) or []
        except Exception as e:
            print(f"[记忆·手册] 加载失败（{self.path.name}）: {e}")
            return []

    def reload(self) -> int:
        self.manuals = self._load()
        return len(self.manuals)

    def search(self, query: str, top_k: int = 2) -> list[dict]:
        """检索相关手册。命中的每个条目附带 `_score`，便于前端/日志说明为何命中。"""
        if not query or not self.manuals:
            return []
        scored: list[tuple[float, dict]] = []
        for m in self.manuals:
            score = 0.0
            for kw in m.get("keywords", []):
                if kw and kw.lower() in query.lower():
                    score += 2.0
            if m.get("name", "") and m["name"] in query:
                score += 3.0
            score += keyword_score(str(m.get("content", "")), query)
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, m in scored[:top_k]:
            item = dict(m)
            item["_score"] = round(score, 2)
            out.append(item)
        return out

    def get_context(self, query: str, top_k: int = 2) -> str:
        """拼成可直接注入 prompt 的文本块；无命中返回空串。"""
        manuals = self.search(query, top_k=top_k)
        if not manuals:
            return ""
        parts = ["【设备知识参考】"]
        for m in manuals:
            parts.append(f"- {m.get('name', '')}: {m.get('content', '')}")
            if m.get("energy_tip"):
                parts.append(f"  节能提示: {m['energy_tip']}")
        return "\n".join(parts)


manuals = ManualStore()
