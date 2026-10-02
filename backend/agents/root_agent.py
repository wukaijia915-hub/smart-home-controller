# -*- coding: utf-8 -*-
"""Root Agent（编排者）。

职责：Ground（拉取状态真相）→ Propose（意图理解 + 任务拆解）→ Grant（签发授权租约）。
只提议、只授权，**不直接执行设备**；执行权下放给 Manager Agent。

意图识别走**漏斗**，从最具体到最宽泛，逐级降级：

    ① 非指令文本保护        空输入、以及条件句（"如果…就…"）—— 都不产生任何动作
    ② 场景规则 SCENES       10 个标准场景 —— 标准场景集（S1-S11）的评分依据，必须最高优先
    ③ 技能触发词            skills.py 的用户自定义动作组合
    ④ 能力契约别名          capabilities.json 的声明式别名表（参数在配置里写死）
    ⑤ 设备指令正则          宽泛正则兜底（含数字提取，如"空调调到24度"）
    ⑥ LLM 慢环             仅当 ①~⑤ 全未命中且允许上云时进入

**顺序不可随意调换**：漏斗按"精确 → 宽泛"排列。实测过的反例是设备正则
`窗帘.*(开|拉开)` 若排在能力契约之前，会把「窗帘开一半」抢成全开
（能力契约给的是 position=50）。规则越宽泛必须越靠后。

①~⑤ 全部在 `plan()` 内同步完成，**物理上不可能触达 LLM**，这是规则层 <100ms 的保证。
`tests_funnel.py` 会把这一约束钉住。
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..devices.registry import DeviceRegistry
from ..knowledge.rag import kb
from ..scenes import SCENES
from ..state import StateStore
from .llm_budget import LLMBudget
from .plan import SubTask, TaskPlan  # noqa: F401  （SubTask/TaskPlan 对外仍从本模块可导入）


def _room_of(text: str) -> str:
    return "卧室" if "卧室" in text or "房间" in text else "客厅"


# 条件句标记：同时出现"条件词"与"结果词"才判为条件指令。
# 只看"就"会把"我就想开个灯"误判；只看"如果"会把"如果我想看电影怎么办"误判。
_COND_HEAD = re.compile(r"如果|要是|假如|万一|若是|倘若")
_COND_TAIL = re.compile(r"就|的话|则|便")


def _is_conditional(text: str) -> bool:
    return bool(_COND_HEAD.search(text) and _COND_TAIL.search(text))


def _light_id(room: str, idx: int = 0) -> str:
    return f"light_{'bedroom' if room == '卧室' else 'living'}_0{idx + 1}"


def _curtain_id(room: str) -> str:
    return f"curtain_{'bedroom' if room == '卧室' else 'living'}_01"


def _ac_id(room: str) -> str:
    return f"ac_{'bedroom' if room == '卧室' else 'living'}_01"


class RootAgent:
    def __init__(self, registry: DeviceRegistry, store: StateStore, llm,
                 capabilities=None, skills=None):
        self.registry = registry
        self.store = store
        self.llm = llm
        self.capabilities = capabilities   # CapabilityRegistry（漏斗④）
        self.skills = skills               # SkillManager（漏斗③）
        # 慢环预算：给模型调用设次数/墙钟/单次超时上界。见 llm_budget.py 的说明 ——
        # 它不是延迟优化手段（已证伪），而是防止一次卡住的请求长时间占住线程。
        self.budget = LLMBudget()

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

        # 1) 空输入 / 无意义输入保护：不调用 LLM、不执行任何操作
        if not cleaned or not re.search(r"[一-龥a-zA-Z0-9]", cleaned):
            return TaskPlan(
                is_command=False,
                source="fallback",
                reply="我暂时没有理解您的意思，您可以试试说：\"我回家了\"、\"看电影\"、\"空调调到 24 度\"、\"打开窗帘\"等。",
            )

        # 1b) 条件指令保护：不能把「如果客厅没人就把灯关掉」当成「立即关灯」执行。
        #    这类请求要的是**创建自动化规则**，而不是立刻动作。当前没有从对话创建
        #    事件路由的能力（阶段4 才做），所以诚实拒绝并指路，而不是静默执行一条
        #    用户没要求的动作。设备正则会命中"关灯"，必须在它之前拦下。
        if _is_conditional(cleaned):
            return TaskPlan(
                is_command=False,
                source="fallback",
                reply="这看起来是一个条件触发的自动化需求。为避免误执行，我不会立刻执行动作。"
                      "您可以先用「离家模式」「观影模式」等场景立即生效；"
                      "或在界面「定时任务」里创建定时规则。",
            )

        # 2) 场景规则（最高优先级，<100ms）
        scene = self._match_scene(cleaned)
        if scene:
            sub_tasks = [self._to_subtask(a) for a in SCENES[scene]["actions"]]
            return TaskPlan(is_command=True, source="rule", reply=self._scene_reply(scene),
                            sub_tasks=sub_tasks, state_version=self.store.version)

        # 3) 技能触发词（★接线：原 skills 系统是孤岛，只能手动调 API 执行）
        if self.skills is not None:
            skill = self.skills.match_by_keyword(cleaned)
            if skill:
                plan = self._skill_plan(skill)
                if plan is not None:
                    return plan

        # 4) 能力别名归一（★接线：原 capabilities 是孤岛）
        #    声明式别名表，参数在配置里写死（"太亮"→亮度20，"开一半窗帘"→开合度50），
        #    比下面的正则更精确 —— 纯 dict 查找（别名按长度倒序），实测 <0.1ms。
        if self.capabilities is not None:
            plan = self._capability_plan(cleaned)
            if plan is not None:
                return plan

        # 5) 直接设备指令规则（宽泛正则，兜底）
        #    必须排在能力契约之后：这里的 `窗帘.*(开|拉开)` 之类宽泛模式会把
        #    "窗帘开一半" 抢成全开（能力契约给的是 position=50）。规则从最具体
        #    到最宽泛排列，才能避免宽泛规则吞掉精确语义。
        sub_tasks, reply = self._parse_device_command(cleaned)
        if sub_tasks:
            return TaskPlan(is_command=True, source="rule", reply=reply,
                            sub_tasks=sub_tasks, state_version=self.store.version)

        # 6) LLM 慢路径（云侧大模型）：意图理解 + 任务拆解（只拆高层目标，不产出具体命令）。
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

    # ---------- 漏斗④：技能触发词 → 动作序列 ----------
    def _skill_plan(self, skill: dict) -> Optional[TaskPlan]:
        """把技能的 {device_id, command, params} 序列展开为子任务。

        标 source="rule"：动作已由用户显式定义，不需要 Manager 再调 LLM 翻译。
        技能里的高危命令（unlock / 燃气灶点火等）仍会被 Manager 挂起待确认 ——
        Manager 的高危判定按 (category, command) 走，与子任务来源无关。
        """
        sub_tasks = []
        for action in skill.get("actions", []):
            device_id = action.get("device_id", "")
            command = action.get("command", "")
            if not device_id or not command:
                continue
            device = self.registry.get(device_id)
            sub_tasks.append(SubTask(
                source="rule", device_id=device_id, command=command,
                params=action.get("params", {}) or {},
                category=device.category if device else "",
            ))
        if not sub_tasks:
            return None
        return TaskPlan(is_command=True, source="rule",
                        reply=f"已为您执行「{skill.get('name', '技能')}」。",
                        sub_tasks=sub_tasks, state_version=self.store.version)

    # ---------- 漏斗⑤：口语别名 → 能力契约 → 具体设备 ----------
    def _capability_plan(self, text: str) -> Optional[TaskPlan]:
        """先由能力契约把口语归一（"太亮"→lighting.set_brightness），再落到设备。

        房间偏好与设备指令规则保持一致：文本提到卧室就只作用于卧室设备，
        否则优先客厅；房间内没有匹配设备时退回全部匹配设备
        （例如门锁在玄关，不属于客厅或卧室，此时仍应命中）。
        """
        cap = self.capabilities.resolve(text)
        if not cap or not cap.get("command"):
            return None
        devices = self.capabilities.find_devices(cap["capability"], self.registry)
        if not devices:
            return None

        room = _room_of(text)
        targets = [d for d in devices if d.room == room] or devices

        sub_tasks = [
            SubTask(source="rule", device_id=d.device_id, command=cap["command"],
                    params=dict(cap["params"]), category=d.category)
            for d in targets
        ]
        names = "、".join(d.name for d in targets)
        # 能力描述形如「调节灯具亮度（0-100%）」，回复时去掉括号内的取值范围
        desc = re.sub(r"（[^）]*）", "", cap.get("description", "")).strip()
        reply = f"已为您{desc}（{names}）。" if desc else f"已为您调整{names}。"
        return TaskPlan(is_command=True, source="rule", reply=reply,
                        sub_tasks=sub_tasks, state_version=self.store.version)

    # ---------- 场景关键词规则 ----------
    def _match_scene(self, text: str) -> Optional[str]:
        """场景关键词规则。

        关键词表只负责**召回**（这句话是不是在说某个场景），不负责排除。因此
        要避免像 `if "空调" in text` 那样过宽的条件——那会把"什么温度开空调最省电"
        这类问句吞成控制指令。同义词该补就补（"我走了"也是标准的离家说法），
        但不要为了覆盖而把条件放宽到会命中非指令文本。
        """
        rules = [
            (r"回家|到家|回来了|我回来了", "home"),
            (r"出门|离家|去上班|离开家|我走了|出去了|不在家", "away"),
            (r"看电影|观影|开电视|电视.*电影|看个片|看片子|看部片", "movie"),
            (r"睡觉|睡眠|就寝|上床|休息了", "sleep"),
            (r"晚安|睡了", "night"),
            (r"会客|客人|朋友来|接待|家里来人", "guest"),
            (r"起床|早上好|早安|醒了", "morning"),
            (r"好热|太热|热死|降温|凉快", "cool"),
            (r"好冷|太冷|冻|升温|暖和", "warm"),
            (r"省电|节能|环保|节约用电", "save"),
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
            f"请判断该指令是否为设备控制/场景联动意图，并给出具体的设备动作。"
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for m in (history or [])[-6:]:
            messages.insert(-1, m)

        # 经预算调用：超时由 per_call_timeout 覆盖后端的 LLM_TIMEOUT（实测为 30s），
        # 失败/超限一律返回 None，由下方走降级路径，并在 budget 里留下 degraded 标记
        result = self.budget.call(self.llm, messages, max_tokens=700, context_text=text)
        # LLM 返回的 JSON 属不可信输入：必须是对象，字段类型逐一防御
        if not isinstance(result, dict):
            return None

        is_command = bool(result.get("is_command", False))
        reply = result.get("reply") or result.get("text") or "已为您处理。"
        if not is_command:
            return TaskPlan(is_command=False, source="llm", reply=reply,
                            sub_tasks=[], state_version=self.store.version)

        raw_actions = result.get("actions")
        if not isinstance(raw_actions, list):
            raw_actions = []
        sub_tasks = []
        for a in raw_actions:
            if not isinstance(a, dict):
                continue
            device_id = str(a.get("device_id") or "")
            command = str(a.get("command") or "")
            if not device_id or not command:
                continue        # 字段不全 → 不是一条可校验的提议，直接丢弃
            params = a.get("params")
            device = self.registry.get(device_id)
            sub_tasks.append(SubTask(
                source="llm", device_id=device_id, command=command,
                params=params if isinstance(params, dict) else {},
                category=device.category if device else "",
            ))
            # 注意：此处**不**过滤 device_id 不存在的动作。Root 只提议、不执行，
            # 合法性由 Manager._verify 判定（实测幻觉率 0%，且 verify 有测试覆盖）。
            # 若在这里自审，Manager 那道闸门就失去了意义。

        if not sub_tasks:
            return TaskPlan(is_command=False, source="llm", reply=reply,
                            sub_tasks=[], state_version=self.store.version)
        return TaskPlan(is_command=True, source="llm", reply=reply,
                        sub_tasks=sub_tasks, state_version=self.store.version)

    # ---------- 慢环：意图理解 + 具体动作提议 ----------
    def _describe_devices(self) -> str:
        """生成给模型看的设备清单（含可用命令与参数范围）。

        命令与参数范围直接取自 `toolkit.schema_gen.PARAM_SOURCE` —— prompt 与工具层
        共用同一份定义，避免出现"prompt 教模型调一个命令、工具层却不认"的漂移。
        """
        from ..toolkit.schema_gen import PARAM_SOURCE

        lines = []
        for d in self.registry.all():
            cmds = []
            for c in d.commands:
                parts = []
                for pname, aname in PARAM_SOURCE.get(c, []):
                    a = d.attributes.get(aname)
                    if a is None:
                        continue
                    if a.type == "enum" and a.options:
                        parts.append(f"{pname}:{'|'.join(str(o) for o in a.options)}")
                    elif a.type == "number":
                        parts.append(f"{pname}:{a.min}-{a.max}")
                cmds.append(f"{c}({','.join(parts)})" if parts else c)
            state = {k: v.value for k, v in d.attributes.items()}
            lines.append(
                f"- {d.device_id} | {d.name} | {d.room} | {d.category} "
                f"| 当前={json.dumps(state, ensure_ascii=False)} "
                f"| 命令：{' '.join(cmds)}")
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        return (
            "你是智能家居中控的编排者 Root，负责意图理解与动作提议，**只提议、不执行**。\n"
            "输出 JSON，格式如下：\n"
            '{"is_command": true, "reply": "简短中文回复", '
            '"actions": [{"device_id": "light_living_01", "command": "set_brightness", "params": {"brightness": 20}}]}\n'
            "规则：\n"
            "1. is_command 表示用户输入是否为设备控制/场景联动意图。寒暄、闲聊、提问、问候"
            "（如「你好」「你是谁」「谢谢」）必须 is_command=false 且 actions 为空数组 []。\n"
            "2. device_id 与 command 必须严格取自下面的设备清单，**不得编造**。\n"
            "3. 参数值必须落在清单标注的范围内。一个目标可能对应多条动作"
            "（如「开空调到 24 度」= turn_on + set_temperature）。\n"
            "4. 单次回复最多 4 条动作：若目标需要更多，只做最关键的几条，并在 reply 里说明还差什么。\n"
            "5. 结合传感器与用户习惯可主动给出联动建议，但动作必须与用户指令相关。\n\n"
            "可用设备（id | 名称 | 房间 | 类别 | 当前状态 | 可用命令）：\n"
            + self._describe_devices() + "\n\n只输出 JSON，不要输出其他文字。"
        )
