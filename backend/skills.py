# -*- coding: utf-8 -*-
"""Skills 技能系统：用户可自定义的自动化动作组合（一组设备操作的序列）。

与场景 / 事件路由的职责边界：
- `scenes.py`    预置场景（10 个固定场景，规则层直接命中）
- `skills.py`    用户自定义组合（本模块，可由关键字触发或手动执行）
- `event_routes.json`  事件触发的自动联动

三者数据结构统一为 `{device_id, command, params}`，便于互相转换。

动作格式（v2）：
    {"device_id": "light_living_01", "command": "turn_off", "params": {}}

v1 的 `{"device_id": "light_living", "command": {"power": "off"}}` 已废弃 ——
device_id 缺 `_01` 后缀、command 是个 dict 而非命令字符串，
与 DeviceRegistry.control 的签名不符，导致技能执行必然抛异常。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Optional

from .lease import LeaseValidator

logger = logging.getLogger("skills")

SKILLS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "skills.json")
FORMAT_VERSION = 2

BUILTIN_SKILLS: dict[str, dict] = {
    "movie_night": {
        "id": "movie_night",
        "name": "电影之夜",
        "description": "关闭主灯、拉上窗帘、打开电视并调低音量、空调调至 24 度",
        # 触发词留空：本技能是 scenes.py 中 movie 场景的加强版，若配"看电影"类
        # 触发词会被场景规则（漏斗②）先截胡，成为永不命中的死条目。
        # 保留它作为「多设备序列」的格式示例，可通过 API 显式执行，或由用户
        # 复制后自定义触发词。技能触发词与场景关键词的重叠由 tests_skills.py 校验。
        "trigger_keywords": [],
        "actions": [
            {"device_id": "light_living_01", "command": "turn_off", "params": {}},
            {"device_id": "light_living_02", "command": "turn_off", "params": {}},
            {"device_id": "curtain_living_01", "command": "close", "params": {}},
            {"device_id": "tv_living_01", "command": "turn_on", "params": {}},
            {"device_id": "tv_living_01", "command": "set_volume", "params": {"volume": 30}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 24}},
        ],
        "enabled": True,
        "built_in": True,
    },
    "good_night": {
        "id": "good_night",
        "name": "晚安模式",
        "description": "关闭客厅全部设备、卧室灯调至微光、卧室空调设为 26 度",
        # 触发词留空：同 movie_night（"晚安/睡了" 已被 night 场景规则占用）
        "trigger_keywords": [],
        "actions": [
            {"device_id": "light_living_01", "command": "turn_off", "params": {}},
            {"device_id": "light_living_02", "command": "turn_off", "params": {}},
            {"device_id": "tv_living_01", "command": "turn_off", "params": {}},
            {"device_id": "ac_living_01", "command": "turn_off", "params": {}},
            {"device_id": "light_bedroom_01", "command": "set_brightness", "params": {"brightness": 10}},
            {"device_id": "ac_bedroom_01", "command": "set_temperature", "params": {"temperature": 26}},
        ],
        "enabled": True,
        "built_in": True,
    },
    "welcome_home": {
        "id": "welcome_home",
        "name": "欢迎回家",
        "description": "打开客厅灯、拉开窗帘、空调调至 24 度",
        # 触发词留空：同 movie_night（"回家/到家" 已被 home 场景规则占用）
        "trigger_keywords": [],
        "actions": [
            {"device_id": "light_living_01", "command": "set_brightness", "params": {"brightness": 80}},
            {"device_id": "curtain_living_01", "command": "open", "params": {}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 24}},
        ],
        "enabled": True,
        "built_in": True,
    },
    # 用户自定义技能的示例：触发词刻意避开场景规则关键词，能被漏斗④ 命中。
    "reading_mode": {
        "id": "reading_mode",
        "name": "阅读模式",
        "description": "客厅灯调至 70% 暖白光、关电视、空调 25 度静音运行",
        "trigger_keywords": ["阅读模式", "看书模式", "我要看书"],
        "actions": [
            {"device_id": "light_living_01", "command": "set_brightness", "params": {"brightness": 70}},
            {"device_id": "light_living_01", "command": "set_color_temp", "params": {"color_temp": 4500}},
            {"device_id": "tv_living_01", "command": "turn_off", "params": {}},
            {"device_id": "ac_living_01", "command": "set_temperature", "params": {"temperature": 25}},
            {"device_id": "ac_living_01", "command": "set_fan_speed", "params": {"fan_speed": "low"}},
        ],
        "enabled": True,
        "built_in": True,
    },
}

# 触发词约束：技能（漏斗④）在场景（漏斗②）之后，重叠的触发词永远不会被走到。
# 内置的三个技能因与场景语义重复，触发词一律留空；reading_mode 演示了
# 一个不重叠的自定义技能应该长什么样。tests_skills.py 会校验这一约束。


class SkillManager:
    def __init__(self):
        self._skills: dict[str, dict] = {}
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        loaded: dict[str, dict] = {}
        if os.path.exists(SKILLS_FILE):
            try:
                with open(SKILLS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                version = data.get("format_version") if isinstance(data, dict) else None
                skills = data.get("skills") if isinstance(data, dict) else data
                if version == FORMAT_VERSION and isinstance(skills, dict):
                    loaded = skills
                else:
                    logger.warning(
                        f"技能库格式为 v{version}（当前 v{FORMAT_VERSION}），丢弃旧数据")
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"技能库读取失败，丢弃旧数据: {e}")

        # 内置技能始终以代码为准覆盖：否则改了 BUILTIN_SKILLS 也不会生效
        # （磁盘上的旧副本会把它盖回去）。用户自建技能原样保留。
        merged = {k: v for k, v in loaded.items() if not v.get("built_in")}
        merged.update(json.loads(json.dumps(BUILTIN_SKILLS)))
        self._skills = merged
        self._save()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(SKILLS_FILE), exist_ok=True)
        with open(SKILLS_FILE, "w", encoding="utf-8") as f:
            json.dump({"format_version": FORMAT_VERSION, "skills": self._skills},
                      f, ensure_ascii=False, indent=2)

    # ---------- 增删查 ----------
    def create(self, skill: dict) -> str:
        skill_id = skill.get("id") or f"skill_{uuid.uuid4().hex[:8]}"
        skill["id"] = skill_id
        skill["enabled"] = skill.get("enabled", True)
        skill["built_in"] = skill.get("built_in", False)
        skill["actions"] = [self._normalize_action(a) for a in skill.get("actions", [])]
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
        """按触发词匹配技能。长触发词优先，避免短词截胡。"""
        if not text:
            return None
        best: Optional[tuple[int, dict]] = None
        for skill in self._skills.values():
            if not skill.get("enabled"):
                continue
            for kw in skill.get("trigger_keywords", []):
                if kw and kw in text:
                    if best is None or len(kw) > best[0]:
                        best = (len(kw), skill)
        return best[1] if best else None

    # ---------- 执行 ----------
    def execute(self, skill_id: str, registry) -> dict:
        """执行技能。

        **高危命令一律拒绝**：技能是"一键触发一串动作"的批量入口，
        若允许它直接下发 unlock / 燃气灶点火，就绕过了 Manager 的二次确认闸门。
        高危动作必须由用户单独下达指令，走正常的高危挂起流程。
        """
        skill = self._skills.get(skill_id)
        if not skill:
            return {"ok": False, "error": f"技能不存在: {skill_id}"}
        if not skill.get("enabled"):
            return {"ok": False, "error": f"技能已禁用: {skill['name']}"}

        results = []
        for action in skill.get("actions", []):
            device_id = action.get("device_id", "")
            command = action.get("command", "")
            params = action.get("params", {}) or {}
            device = registry.get(device_id)

            if device is None:
                results.append({"device_id": device_id, "command": command, "ok": False,
                                "error": f"设备不存在: {device_id}"})
                continue
            if LeaseValidator.is_high_risk(device.category, command):
                desc = LeaseValidator.high_risk_desc(device.category, command)
                results.append({"device_id": device_id, "command": command, "ok": False,
                                "error": f"「{desc}」属高危操作，不能通过技能批量执行，"
                                         f"请单独下达指令以触发二次确认"})
                continue
            results.append(registry.control(device_id, command, params))

        succeeded = sum(1 for r in results if r.get("ok"))
        logger.info(f"执行技能 {skill['name']}: {succeeded}/{len(results)} 个动作成功")
        return {"ok": True, "skill": skill["name"], "total": len(results),
                "succeeded": succeeded, "actions": results}

    @staticmethod
    def _normalize_action(action: dict) -> dict:
        """把动作规整为 {device_id, command, params}。

        v1 的 command 是 dict（如 {"power": "off"}），无法映射为单一命令 —
        这类历史数据在 _load 阶段已被 format_version 检查拦掉，此处只做 shape 保证。
        """
        command = action.get("command", "")
        if isinstance(command, dict):
            command = ""
        return {
            "device_id": action.get("device_id", ""),
            "command": command,
            "params": action.get("params", {}) or {},
        }


# 全局单例
skill_manager = SkillManager()
