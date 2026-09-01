# -*- coding: utf-8 -*-
"""轻量级 RAG 知识库：设备说明书检索 + 用户习惯学习。
无需向量数据库，采用关键词匹配 + 习惯统计，适合本地轻量部署。
- 设备说明书：根据用户指令关键词检索相关设备知识，注入 LLM prompt
- 用户习惯：记录用户历史操作偏好，统计频率，在决策时参考
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional

KNOWLEDGE_DIR = Path(__file__).resolve().parent
MANUALS_PATH = KNOWLEDGE_DIR / "device_manuals.json"
HABITS_PATH = KNOWLEDGE_DIR / "user_habits.json"


class KnowledgeBase:
    def __init__(self):
        self.manuals = self._load_manuals()
        self.habits = self._load_habits()

    def _load_manuals(self) -> list[dict]:
        if not MANUALS_PATH.exists():
            return []
        try:
            with open(MANUALS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("manuals", [])
        except Exception as e:
            print(f"[知识库] 设备说明书加载失败: {e}")
            return []

    def _load_habits(self) -> dict:
        if not HABITS_PATH.exists():
            return {"commands": {}, "scenes": {}, "preferences": {}}
        try:
            with open(HABITS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[知识库] 用户习惯加载失败: {e}")
            return {"commands": {}, "scenes": {}, "preferences": {}}

    def _save_habits(self):
        try:
            with open(HABITS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.habits, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[知识库] 用户习惯保存失败: {e}")

    def search_manuals(self, query: str, top_k: int = 2) -> list[dict]:
        """根据用户指令关键词检索设备说明书。"""
        if not query or not self.manuals:
            return []
        query = query.lower()
        scored = []
        for m in self.manuals:
            score = 0
            # 关键词匹配
            for kw in m.get("keywords", []):
                if kw in query:
                    score += 2
            # 名称匹配
            if m.get("name", "") in query:
                score += 3
            # 内容关键词匹配
            content = m.get("content", "").lower()
            for word in re.findall(r'[\u4e00-\u9fa5]{2,}', query):
                if word in content:
                    score += 1
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:top_k]]

    def get_context(self, query: str) -> str:
        """生成注入 LLM prompt 的知识上下文。"""
        parts = []
        # 设备说明书
        manuals = self.search_manuals(query, top_k=2)
        if manuals:
            parts.append("【设备知识参考】")
            for m in manuals:
                parts.append(f"- {m['name']}: {m['content']}")
                if m.get("energy_tip"):
                    parts.append(f"  节能提示: {m['energy_tip']}")
        # 用户习惯
        habit_hint = self.get_habit_hint(query)
        if habit_hint:
            parts.append(f"【用户习惯参考】{habit_hint}")
        return "\n".join(parts) if parts else ""

    def record_command(self, device_id: str, command: str, params: dict):
        """记录用户设备操作习惯。"""
        key = f"{device_id}:{command}"
        self.habits.setdefault("commands", {})
        self.habits["commands"][key] = self.habits["commands"].get(key, 0) + 1
        # 记录参数偏好
        if params:
            pref_key = f"{device_id}:{command}:params"
            self.habits.setdefault("preferences", {})
            self.habits["preferences"].setdefault(pref_key, [])
            self.habits["preferences"][pref_key].append(params)
            # 只保留最近 20 条
            self.habits["preferences"][pref_key] = self.habits["preferences"][pref_key][-20:]
        self._save_habits()

    def record_scene(self, scene: str):
        """记录用户场景使用习惯。"""
        self.habits.setdefault("scenes", {})
        self.habits["scenes"][scene] = self.habits["scenes"].get(scene, 0) + 1
        self._save_habits()

    def get_habit_hint(self, query: str) -> str:
        """根据用户指令生成习惯参考提示。"""
        hints = []
        # 最常用场景
        scenes = self.habits.get("scenes", {})
        if scenes:
            top_scene = max(scenes, key=scenes.get)
            if scenes[top_scene] >= 3:
                hints.append(f"用户最常用场景是「{top_scene}」(使用{scenes[top_scene]}次)")
        # 最常用设备操作
        commands = self.habits.get("commands", {})
        if commands:
            top_cmd = max(commands, key=commands.get)
            if commands[top_cmd] >= 3:
                device, cmd = top_cmd.split(":")
                hints.append(f"用户频繁操作「{device}」的「{cmd}」({commands[top_cmd]}次)")
        return "；".join(hints) if hints else ""

    def get_stats(self) -> dict:
        """获取知识库统计信息。"""
        return {
            "manuals_count": len(self.manuals),
            "total_commands": sum(self.habits.get("commands", {}).values()),
            "total_scenes": sum(self.habits.get("scenes", {}).values()),
            "top_scene": max(self.habits.get("scenes", {}), key=self.habits.get("scenes", {}).get, default=None),
        }


# 全局单例
kb = KnowledgeBase()
