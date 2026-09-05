# -*- coding: utf-8 -*-
"""Root Agent（编排者）。

职责：Ground（拉取状态真相）→ Propose（意图理解 + 任务拆解）→ Grant（签发授权租约）。
只提议、只授权，**不直接执行设备**；执行权下放给 Manager Agent。
规则快路径（<100ms）与 LLM 慢路径（复杂/模糊指令）双轨。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..devices.registry import DeviceRegistry
from ..knowledge.rag import kb
from ..scenes import SCENES
from ..state import StateStore


def _room_of(text: str) -> str:
    return "卧室" if "卧室" in text or "房间" in text else "客厅"


def _light_id(room: str, idx: int = 0) -> str:
    return f"light_{'bedroom' if room == '卧室' else 'living'}_0{idx + 1}"


def _curtain_id(room: str) -> str:
    return f"curtain_{'bedroom' if room == '卧室' else 'living'}_01"


def _ac_id(room: str) -> str:
    return f"ac_{'bedroom' if room == '卧室' else 'living'}_01"


@dataclass
class SubTask:
    """一个子任务。

    source="rule" 时 device_id/command/params 已由规则填好；
    source="llm"  时只有 goal（高层意图），由 Manager 翻译成具体动作。
    """
    source: str = "rule"                 # rule | llm
    device_id: str = ""
    command: str = ""
    params: dict = field(default_factory=dict)
    category: str = ""                   # 设备类别（风险判定用）
    goal: str = ""                       # llm 路径：高层意图描述


@dataclass
class TaskPlan:
    is_command: bool
    reply: str
    sub_tasks: list = field(default_factory=list)
    state_version: int = 0
    source: str = "rule"                 # rule | llm | fallback


class RootAgent:
    def __init__(self, registry: DeviceRegistry, store: StateStore, llm):
        self.registry = registry
        self.store = store
        self.llm = llm

    # ---------- Ground：拉取状态真相 ----------
    def ground(self, sensors: Optional[dict] = None) -> dict:
        return {
            "devices": self.registry.snapshot(),
            "sensors": sensors or {},
            "state_version": self.store.version,
        }

    # ---------- 决策入口 ----------
    def plan(self, text: str, sensors: Optional[dict] = None, history: Optional[list] = None,
             allow_cloud: bool = True, cloud_text: Optional[str] = None) -> TaskPlan:
        """allow_cloud=False 时禁止调用云端大模型（L0 高敏数据强制端侧本地处理）。
        cloud_text 为上云前脱敏后的文本，仅用于云侧 LLM 推理，规则层仍用原文。"""
        cleaned = (text or "").strip()

        # 0) 空输入 / 无意义输入保护：不调用 LLM、不执行任何操作
        if not cleaned or not re.search(r"[一-龥a-zA-Z0-9]", cleaned):
            return TaskPlan(
                is_command=False,
                source="fallback",
                reply="我暂时没有理解您的意思，您可以试试说：\"我回家了\"、\"看电影\"、\"空调调到 24 度\"、\"打开窗帘\"等。",
            )

        # 1) 场景规则（最高优先级，<100ms）
        scene = self._match_scene(cleaned)
        if scene:
            sub_tasks = [self._to_subtask(a) for a in SCENES[scene]["actions"]]
            return TaskPlan(is_command=True, source="rule", reply=self._scene_reply(scene),
                            sub_tasks=sub_tasks, state_version=self.store.version)

        # 2) 直接设备指令规则
        sub_tasks, reply = self._parse_device_command(cleaned)
        if sub_tasks:
            return TaskPlan(is_command=True, source="rule", reply=reply,
                            sub_tasks=sub_tasks, state_version=self.store.version)

        # 3) LLM 慢路径（云侧大模型）：意图理解 + 任务拆解（只拆高层目标，不产出具体命令）。
        #    L0 高敏数据（allow_cloud=False）禁止上云，此处直接跳过。
        if allow_cloud:
            plan = self._llm_plan(cloud_text or cleaned, sensors, history)
            if plan is not None:
                return plan

        # 4) 兜底
        return TaskPlan(
            is_command=False,
            source="fallback",
            reply="我暂时没有理解您的意思，您可以试试说：\"我回家了\"、\"看电影\"、\"空调调到 24 度\"、\"打开窗帘\"等。",
        )

    def _to_subtask(self, action: dict) -> SubTask:
        device = self.registry.get(action.get("device_id", ""))
        return SubTask(
            source="rule",
            device_id=action.get("device_id", ""),
            command=action.get("command", ""),
            params=action.get("params", {}),
            category=device.category if device else "",
        )

    # ---------- 场景关键词规则 ----------
    def _match_scene(self, text: str) -> Optional[str]:
        rules = [
            (r"回家|到家|回来了", "home"),
            (r"出门|离家|去上班|离开家", "away"),
            (r"看电影|观影|开电视|电视.*电影", "movie"),
            (r"睡觉|睡眠|就寝|上床", "sleep"),
            (r"晚安|睡了", "night"),
            (r"会客|客人|朋友来|接待", "guest"),
            (r"起床|早上好|早安", "morning"),
            (r"好热|太热|热死|降温|凉快", "cool"),
            (r"好冷|太冷|冻|升温|暖和", "warm"),
            (r"省电|节能|环保", "save"),
        ]
        for pattern, scene in rules:
            if re.search(pattern, text):
                return scene
        return None

    def _scene_reply(self, scene: str) -> str:
        replies = {
            "home": "欢迎回家！已为您打开客厅灯、拉开窗帘，并开启空调调节到舒适温度。",
            "away": "好的，已为您关闭所有灯光与家电，并拉好窗帘，进入离家模式。",
            "movie": "观影模式已就绪：灯光调暗、窗帘关闭、电视已开启，享受电影吧。",
            "sleep": "晚安模式：已关闭主灯，保留夜灯微光，空调切换为自动睡眠模式。",
            "night": "已为您关闭所有设备并拉好窗帘，晚安。",
            "guest": "会客模式已开启：灯光调亮、窗帘拉开、空调调至舒适温度。",
            "morning": "早上好！卧室灯光已渐亮，窗帘已打开，开启元气满满的一天。",
            "cool": "已为您开启空调降温，并打开风扇辅助循环。",
            "warm": "已为您开启空调制热模式，室内将逐渐回暖。",
            "save": "已进入节能模式：关闭非必要设备，空调切换为节能运行。",
        }
        return replies.get(scene, f"已为您切换到{SCENES.get(scene, {}).get('desc', scene)}。")

    # ---------- 直接设备指令规则 ----------
    def _parse_device_command(self, text: str):
        """解析直接设备指令（支持一句话多条指令），返回 (sub_tasks, reply)。"""
        actions = []
        replies = []
        room = _room_of(text)

        # ---- 灯光开关 ----
        if re.search(r"关.*灯|灯.*关(?!系)|关灯", text) and not re.search(r"开灯|打开.*灯", text):
            for idx in range(2):
                did = _light_id(room, idx)
                if self.registry.get(did):
                    actions.append({"device_id": did, "command": "turn_off", "params": {}})
            if actions:
                replies.append(f"已关闭{room}的灯光。")
        elif re.search(r"开.*灯|灯.*(开|亮)|开灯|打开.*灯", text):
            for idx in range(2):
                did = _light_id(room, idx)
                if self.registry.get(did):
                    actions.append({"device_id": did, "command": "turn_on", "params": {}})
            if actions:
                replies.append(f"已打开{room}的灯光。")

        # ---- 灯光亮度（调暗/调亮/N%） ----
        if re.search(r"调(暗|亮)|灯.*?(\d{1,3})\s*%|灯光.*(暗|亮)", text) and ("灯" in text):
            if re.search(r"调暗|灯光暗|灯.*暗", text) and not re.search(r"调亮", text):
                brightness = 20
            elif re.search(r"调亮|灯光亮|灯.*亮", text) and not re.search(r"调暗", text):
                brightness = 80
            else:
                m = re.search(r"(\d{1,3})\s*%", text)
                brightness = int(m.group(1)) if m else 50
            did = _light_id(room, 0)
            if self.registry.get(did):
                actions.append({"device_id": did, "command": "set_brightness", "params": {"brightness": brightness}})
                replies.append(f"已将{room}灯光亮度调整到 {brightness}%。")

        # ---- 窗帘 ----
        if re.search(r"(开|拉开|打开).*窗帘|窗帘.*(开|拉开)", text):
            did = _curtain_id(room)
            actions.append({"device_id": did, "command": "open", "params": {}})
            replies.append(f"已为您拉开{room}窗帘。")
        elif re.search(r"(关|关闭|拉上).*窗帘|窗帘.*(关|拉上)", text):
            did = _curtain_id(room)
            actions.append({"device_id": did, "command": "close", "params": {}})
            replies.append(f"已为您拉上{room}窗帘。")

        # ---- 空调 ----
        if "空调" in text or "冷气" in text or "暖气" in text:
            did = _ac_id(room)
            ac_on = re.search(r"(开|打开|启动).*空调|空调.*(开|打开)|调到?\s*\d+\s*度|温度", text) and \
                not re.search(r"空调.*(关|关掉)", text)
            ac_off = re.search(r"(关|关闭|关掉).*空调|空调.*(关|关掉)", text)
            if ac_off and not ac_on:
                actions.append({"device_id": did, "command": "turn_off", "params": {}})
                replies.append(f"已关闭{room}空调。")
            elif ac_on or "空调" in text:
                m_temp = re.search(r"(\d{1,2})\s*度", text)
                mode = None
                if re.search(r"制热|暖气|热风|热模式", text):
                    mode = "heat"
                elif re.search(r"制冷|冷气|冷风|冷模式", text):
                    mode = "cool"
                if mode or m_temp or re.search(r"空调.*(开|打开)|打开.*空调", text):
                    actions.append({"device_id": did, "command": "turn_on", "params": {}})
                    if m_temp:
                        actions.append({"device_id": did, "command": "set_temperature", "params": {"temperature": int(m_temp.group(1))}})
                    if mode:
                        actions.append({"device_id": did, "command": "set_mode", "params": {"mode": mode}})
                    detail = f"，温度设为 {int(m_temp.group(1))} 度" if m_temp else ""
                    replies.append(f"已开启{room}空调{detail}。")

        # ---- 电视 ----
        if "电视" in text:
            did = "tv_living_01"
            if re.search(r"(关|关闭|关掉).*电视|电视.*(关|关掉)", text):
                actions.append({"device_id": did, "command": "turn_off", "params": {}})
                replies.append("已关闭电视。")
            else:
                m_vol = re.search(r"音量.*?(\d{1,3})", text)
                if m_vol:
                    actions.append({"device_id": did, "command": "set_volume", "params": {"volume": int(m_vol.group(1))}})
                    replies.append(f"电视音量已调到 {int(m_vol.group(1))}。")
                else:
                    actions.append({"device_id": did, "command": "turn_on", "params": {}})
                    replies.append("已为您打开电视。")

        # ---- 风扇 ----
        if "风扇" in text or "电扇" in text:
            did = "fan_living_01"
            if re.search(r"(关|关闭|关掉).*风扇|风扇.*关", text):
                actions.append({"device_id": did, "command": "turn_off", "params": {}})
                replies.append("已关闭风扇。")
            else:
                actions.append({"device_id": did, "command": "turn_on", "params": {}})
                replies.append("已为您打开风扇。")

        # ---- 加湿器 ----
        if "加湿" in text:
            did = "humidifier_living_01"
            if re.search(r"(关|关闭|关掉).*加湿|加湿.*关", text):
                actions.append({"device_id": did, "command": "turn_off", "params": {}})
                replies.append("已关闭加湿器。")
            else:
                actions.append({"device_id": did, "command": "turn_on", "params": {}})
                replies.append("已为您打开加湿器。")

        if actions:
            return [self._to_subtask(a) for a in actions], " ".join(replies)
        return [], None

    # ---------- LLM 慢路径：意图理解 + 任务拆解 ----------
    def _llm_plan(self, text: str, sensors: Optional[dict], history: Optional[list]) -> Optional[TaskPlan]:
        system = self._build_system_prompt()
        sensor_text = json.dumps(sensors, ensure_ascii=False) if sensors else "{}"
        rag_context = kb.get_context(text)
        rag_section = f"\n\n{rag_context}" if rag_context else ""

        user = (
            f"当前环境传感器：{sensor_text}\n"
            f"{rag_section}\n\n"
            f"用户指令：{text}\n\n"
            f"请判断该指令是否为设备控制/场景联动意图，并拆解为高层子任务。"
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for m in (history or [])[-6:]:
            messages.insert(-1, m)

        result = self.llm.chat_json(messages, max_tokens=500, context_text=text)
        if not result:
            return None

        is_command = bool(result.get("is_command", False))
        reply = result.get("reply") or result.get("text") or "已为您处理。"
        if not is_command:
            return TaskPlan(is_command=False, source="llm", reply=reply,
                            sub_tasks=[], state_version=self.store.version)

        sub_tasks = [
            SubTask(source="llm", goal=t.get("goal", ""), category=t.get("category", ""))
            for t in result.get("sub_tasks", [])
            if t.get("goal")
        ]
        if not sub_tasks:
            return TaskPlan(is_command=False, source="llm", reply=reply,
                            sub_tasks=[], state_version=self.store.version)
        return TaskPlan(is_command=True, source="llm", reply=reply,
                        sub_tasks=sub_tasks, state_version=self.store.version)

    def _build_system_prompt(self) -> str:
        device_desc = []
        for d in self.registry.all():
            attrs = {k: a.value for k, a in d.attributes.items()}
            device_desc.append(f"{d.device_id}({d.name},{d.room},{d.category},{json.dumps(attrs, ensure_ascii=False)})")
        return (
            "你是智能家居中控的编排者 Root，负责意图理解和任务拆解，**只提议、不执行**。\n"
            "输出 JSON，格式如下：\n"
            '{"is_command": true, "reply": "简短中文回复", "sub_tasks": [{"goal": "把客厅灯调暗到20%", "category": "light"}]}\n'
            "规则：\n"
            "1. is_command 表示用户输入是否为设备控制/场景联动意图。寒暄、闲聊、提问、问候（如\"你好\"\"你是谁\"\"谢谢\"）必须 is_command=false 且 sub_tasks 为空数组 []。\n"
            "2. sub_tasks 每个元素是一个高层操作目标（goal），不要写具体 device_id 或 command，由 Manager 负责翻译。\n"
            "3. 结合环境传感器和用户习惯，可主动给出联动建议（如温度高时建议开空调），但 goal 必须与用户指令相关。\n"
            f"可用设备(id,名称,房间,类别,当前属性):\n" + "\n".join(device_desc) + "\n"
            "只输出 JSON，不要输出其他文字。"
        )
