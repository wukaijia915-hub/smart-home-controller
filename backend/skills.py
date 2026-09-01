"""Skills 技能系统：用户可自定义自动化技能（一组设备操作的组合）。
参考 openvela AI Agent 的 Skills 设计，支持自然语言触发和手动执行。
"""
from __future__ import annotations
import json, os, logging
from typing import Optional

logger = logging.getLogger("skills")

SKILLS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "skills.json")


class SkillManager:
    def __init__(self):
        self._skills: dict[str, dict] = {}
        self._load()

    def _load(self):
        if os.path.exists(SKILLS_FILE):
            with open(SKILLS_FILE, "r", encoding="utf-8") as f:
                self._skills = json.load(f)
        else:
            # 内置示例技能
            self._skills = {
                "movie_night": {
                    "id": "movie_night",
                    "name": "电影之夜",
                    "description": "关闭主灯、拉上窗帘、打开电视、空调调至24度",
                    "trigger_keywords": ["电影之夜", "看大片", "影院模式"],
                    "actions": [
                        {"device_id": "light_living", "command": {"power": "off"}},
                        {"device_id": "curtain_living", "command": {"position": 0}},
                        {"device_id": "tv_living", "command": {"power": "on", "volume": 30}},
                        {"device_id": "ac_living", "command": {"power": "on", "mode": "cool", "target_temp": 24}},
                    ],
                    "enabled": True,
                    "built_in": True,
                },
                "good_night": {
                    "id": "good_night",
                    "name": "晚安模式",
                    "description": "关闭客厅所有设备、卧室灯调至微光、空调开启睡眠模式",
                    "trigger_keywords": ["晚安", "睡觉了", "就寝模式"],
                    "actions": [
                        {"device_id": "light_living", "command": {"power": "off"}},
                        {"device_id": "tv_living", "command": {"power": "off"}},
                        {"device_id": "ac_living", "command": {"power": "off"}},
                        {"device_id": "light_bedroom", "command": {"power": "on", "brightness": 10}},
                        {"device_id": "ac_bedroom", "command": {"power": "on", "mode": "cool", "target_temp": 26}},
                    ],
                    "enabled": True,
                    "built_in": True,
                },
                "welcome_home": {
                    "id": "welcome_home",
                    "name": "欢迎回家",
                    "description": "打开客厅灯、拉开窗帘、空调调至舒适温度",
                    "trigger_keywords": ["欢迎回家", "我回来了", "到家了"],
                    "actions": [
                        {"device_id": "light_living", "command": {"power": "on", "brightness": 80}},
                        {"device_id": "curtain_living", "command": {"position": 100}},
                        {"device_id": "ac_living", "command": {"power": "on", "mode": "auto", "target_temp": 24}},
                    ],
                    "enabled": True,
                    "built_in": True,
                },
            }
            self._save()

    def _save(self):
        os.makedirs(os.path.dirname(SKILLS_FILE), exist_ok=True)
        with open(SKILLS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._skills, f, ensure_ascii=False, indent=2)

    def create(self, skill: dict) -> str:
        """创建技能。"""
        import uuid
        skill_id = skill.get("id") or f"skill_{uuid.uuid4().hex[:8]}"
        skill["id"] = skill_id
        skill["enabled"] = skill.get("enabled", True)
        skill["built_in"] = skill.get("built_in", False)
        self._skills[skill_id] = skill
        self._save()
        return skill_id

    def list(self) -> list:
        return list(self._skills.values())

    def get(self, skill_id: str) -> Optional[dict]:
        return self._skills.get(skill_id)

    def delete(self, skill_id: str) -> bool:
        if skill_id in self._skills and not self._skills[skill_id].get("built_in"):
            del self._skills[skill_id]
            self._save()
            return True
        return False

    def match_by_keyword(self, text: str) -> Optional[dict]:
        """根据自然语言匹配技能。"""
        for skill in self._skills.values():
            if not skill.get("enabled"):
                continue
            for kw in skill.get("trigger_keywords", []):
                if kw in text:
                    return skill
        return None

    def execute(self, skill_id: str, registry) -> dict:
        """执行技能，返回执行结果。"""
        skill = self._skills.get(skill_id)
        if not skill:
            return {"ok": False, "error": f"技能不存在: {skill_id}"}
        results = []
        for action in skill.get("actions", []):
            device_id = action.get("device_id")
            command = action.get("command", {})
            ok = registry.execute(device_id, command)
            results.append({"device_id": device_id, "command": command, "ok": ok})
        logger.info(f"执行技能 {skill['name']}: {len(results)} 个操作")
        return {"ok": True, "skill": skill["name"], "actions": results}


# 全局单例
skill_manager = SkillManager()
