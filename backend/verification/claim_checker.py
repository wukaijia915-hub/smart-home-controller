# -*- coding: utf-8 -*-
"""输出层声明校验（claim check）—— 防「嘴上说做完了」。

## 为什么 Manager 之外还需要这一层

Manager 保证的是**执行侧**：LLM 提议的动作要么真执行、要么被拒绝（租约 / 版本 /
参数包络 / 高危确认）。它管不住**回复侧**：模型完全可能只调了灯，却在正文里说
「已把空调调到 26 度」；也可能一个工具都没调，就说「已为您打开客厅灯」。

对照项目 Aether 把这类错误单独做成了一层（`validator_agent.py`）：三时态正则
（已完成 / 进行中 / 将来时）命中表态 → 从上下文提取设备指称 → 匹配实体 →
**读真实状态核对** → 只在「不符」时定向重试。它的实现里有一条原则值得照搬：

> 误报交给下游实体核对兜底，不要为了降误报把正则收窄到漏报。

## 本模块的边界（红线）

- **只读**：只看本轮的 `actions` / `results` / 设备元数据，不执行、不签发租约、
  不 import 设备注册表。`tests_agents.py` 会静态扫描本文件，出现 `registry.control(`、
  `Lease.issue` 或 `from ..devices` 即失败。
- **零 LLM**：全部是确定性规则。它属于规则层，延迟必须是毫秒级。
- **可回退**：`trust.json → claim_check.enabled=false` 整体关闭；`rewrite_reply=false`
  只记 trace 不改文案（现场演示可关）。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

# 配置缓存：claim 校验在**每条决策**上跑（含规则层），绝不能每次都读 JSON。
# 与 devices/registry 的回读参数、toolkit/scoping 的裁剪配置同款 10s TTL。
_CFG_CACHE: dict = {"at": 0.0, "claim": None, "categories": None}
_CFG_TTL = 10.0


def _load_cfg() -> tuple[dict, dict]:
    now = time.monotonic()
    if _CFG_CACHE["claim"] is not None and now - _CFG_CACHE["at"] < _CFG_TTL:
        return _CFG_CACHE["claim"], _CFG_CACHE["categories"] or {}
    claim, categories = {}, {}
    try:
        from ..config_loader import load_trust
        claim = (load_trust() or {}).get("claim_check") or {}
    except Exception:
        claim = {}
    try:
        from ..config_loader import load_tool_scope
        categories = (load_tool_scope() or {}).get("category_keywords") or {}
    except Exception:
        categories = {}
    _CFG_CACHE.update({"at": now, "claim": claim, "categories": categories})
    return claim, categories


def invalidate_claim_cache() -> None:
    """配置热重载后调用。"""
    _CFG_CACHE.update({"at": 0.0, "claim": None, "categories": None})


def load_claim_settings() -> dict:
    """当前生效的 claim_check 配置（供 agent 决定是否改写文案）。"""
    return _load_cfg()[0]

# 完成态动词：只收「动作动词」，不收「完成/搞定/好了」这类通用词 ——
# 它们在闲聊里太常见（"方案好了""计划搞定"），会制造大量误报。
_DONE_VERBS = (
    "打开|开启|开好|关闭|关掉|关上|调节|调整|设置|切换|拉开|拉上|升起|降下"
    "|锁定|解锁|启动|停止|播放|清扫|熄灭|点亮|调到|调成|调至"
)
# 三个分支分别覆盖中文的三种完成态语序：
#   已(经)…打开      → "已为您打开客厅灯"
#   帮你…打开了      → "帮你把灯打开了"
#   关掉了 / 调好了  → "客厅灯关掉了"
_DONE_RE = re.compile(
    r"已(?:经)?[^。！？,，；;\n]{0,8}?(?P<v1>" + _DONE_VERBS + r")"
    r"|[帮给][我你][^。！？,，；;\n]{0,12}?(?P<v2>" + _DONE_VERBS + r")(?:了|好|完|啦)"
    r"|(?P<v3>" + _DONE_VERBS + r")(?:了|好|完|啦)"
)
# 将来时：不是谎言，不判问题（但记下来，便于回答"它是不是只说不做"）。
_FUTURE_RE = re.compile(r"将要|即将|稍后|待会|马上|我会|我来|准备")
# 结构性写入声明：本轮没有真正确认过的草稿/路由时，这类句子必为幻觉。
_CREATE_RE = re.compile(
    r"已(?:经)?[^。！？\n]{0,12}?(?:创建|新建|添加|生成|建立)(?:了)?"
    r"[^。！？\n]{0,12}?(?:规则|场景|技能|自动化|定时任务)"
)

# 动词 → 允许的命令集合。声明"打开"，本轮执行的却是「调亮度」，属于说错方向。
_VERB_COMMANDS = {
    "打开": {"turn_on", "open", "start_stream", "start_clean", "play_music"},
    "开启": {"turn_on", "open", "start_stream", "start_clean", "play_music"},
    "开好": {"turn_on", "open", "start_stream", "start_clean"},
    "点亮": {"turn_on"},
    "关闭": {"turn_off", "close", "stop_stream", "stop_clean", "stop_music"},
    "关掉": {"turn_off", "close", "stop_stream", "stop_clean", "stop_music"},
    "关上": {"close"},
    "熄灭": {"turn_off"},
    "拉开": {"open"},
    "拉上": {"close"},
    "升起": {"open"},
    "降下": {"close"},
    "锁定": {"lock"},
    "解锁": {"unlock"},
    "启动": {"start_stream", "start_clean", "play_music", "turn_on"},
    "停止": {"stop_stream", "stop_clean", "stop_music", "turn_off"},
    "播放": {"play_music"},
    "清扫": {"start_clean"},
    "调节": {"set_brightness", "set_color_temp", "set_temperature", "set_mode",
             "set_fan_speed", "set_volume", "set_speed", "set_target", "set_position",
             "set_flame_level", "set_fan_level"},
    "调整": {"set_brightness", "set_color_temp", "set_temperature", "set_mode",
             "set_fan_speed", "set_volume", "set_speed", "set_target", "set_position",
             "set_flame_level", "set_fan_level"},
    "设置": {"set_brightness", "set_color_temp", "set_temperature", "set_mode",
             "set_fan_speed", "set_volume", "set_speed", "set_target", "set_position",
             "set_flame_level", "set_fan_level"},
    "切换": {"set_mode", "set_speed", "set_clean_mode"},
    "调到": {"set_brightness", "set_color_temp", "set_temperature", "set_mode",
             "set_fan_speed", "set_volume", "set_speed", "set_target", "set_position",
             "set_flame_level", "set_fan_level"},
    "调成": {"set_temperature", "set_mode", "set_speed", "set_clean_mode"},
    "调至": {"set_temperature", "set_mode", "set_speed", "set_brightness", "set_volume"},
}

# 语义相反的动词对：只有"说了开、实际关了"这种明确矛盾才报 command_mismatch。
# 不做"说了开、实际调了亮度"的判定 —— 调亮度常常隐含点亮，把它算矛盾会制造
# 大量误报（对照项目 Aether 的教训：误报交给下游核对兜底，不要为降误报收窄到漏报，
# 但也不要把"不精确"当"矛盾"）。
_OPPOSITE_VERB = {
    "打开": "关闭", "开启": "关闭", "开好": "关闭", "点亮": "熄灭",
    "关闭": "打开", "关掉": "打开", "关上": "拉开", "熄灭": "点亮",
    "拉开": "拉上", "拉上": "拉开", "升起": "降下", "降下": "升起",
    "锁定": "解锁", "解锁": "锁定",
    "启动": "停止", "停止": "启动", "播放": "停止", "清扫": "停止",
}

_PUNCT_RE = re.compile(r"[，。！？!?,.;；:：~～、'\"“”‘’()（）\[\]【】\s]")

# 问题类型 → 严重度。unverified 不是撒谎（确实下发了），所以单独一级。
_SEVERITY = {
    "untouched_device": "high",
    "failed_but_claimed": "high",
    "command_mismatch": "high",
    "creation_without_evidence": "high",
    "unverified": "low",
}


@dataclass
class ClaimIssue:
    kind: str
    claim: str
    detail: str
    device_id: str = ""
    severity: str = "high"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "claim": self.claim, "detail": self.detail,
                "device_id": self.device_id, "severity": self.severity}


@dataclass
class ClaimReport:
    issues: list = field(default_factory=list)
    claims: list = field(default_factory=list)   # 命中的完成态表态原文
    future_claims: list = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def detail(self) -> str:
        if not self.issues:
            return "声明与执行一致"
        parts = [f"{i.detail}" for i in self.issues[:3]]
        more = f" 等 {len(self.issues)} 处" if len(self.issues) > 3 else ""
        return "；".join(parts) + more

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "issues": [i.to_dict() for i in self.issues],
            "claims": list(self.claims),
        }


def _context(text: str, start: int, end: int, before: int = 10, after: int = 16) -> str:
    """取表态前后的小窗口作宾语候选（中文两种语序都会出现）。"""
    return text[max(0, start - before):min(len(text), end + after)]


def _match_devices(context: str, device_meta: dict, category_keywords: dict,
                   touched: Optional[set] = None) -> set[str]:
    """从表态上下文里认出「说的是哪台设备」。

    三级收敛，顺序不能反：
    1. 精确设备名命中（"客厅吊灯"）→ 直接用，不再扩展；
    2. 类别关键词命中（"灯" → 所有 light 设备）；
    3. 用上下文里的房间词收敛（"卧室的灯" 只算卧室的灯）；
    4. 若候选里存在**本轮真的动过**的设备，收敛到这些设备 —— 否则「已开空调」
       会把卧室那台没动过的空调也算成"声称操作了未动作设备"，这是纯粹的误报。

    只有当某个类别里本轮一台设备都没动过时，候选才会保持全量，从而报出
    `untouched_device`（那才是真的说了没做的事）。
    """
    exact = {did for did, m in device_meta.items()
             if (m.get("name") or "") and m["name"] in context}
    if exact:
        return exact

    cats = {cat for cat, keywords in (category_keywords or {}).items()
            if any(str(k) and str(k) in context for k in (keywords or []))}
    matched = {did for did, m in device_meta.items() if m.get("category") in cats}
    if not matched:
        return matched

    rooms = {m.get("room") for m in device_meta.values()
             if m.get("room") and m["room"] in context}
    if rooms:
        narrowed = {did for did in matched if device_meta[did].get("room") in rooms}
        if narrowed:
            matched = narrowed

    if touched:
        both = matched & touched
        if both:
            matched = both
    return matched


def _results_by_device(results: list) -> dict:
    out: dict[str, list] = {}
    for r in results or []:
        if not isinstance(r, dict):
            continue
        did = str(r.get("device_id") or "")
        if did:
            out.setdefault(did, []).append(r)
    return out


def check_claims(reply: str, actions: list, results: list, device_meta: dict,
                 category_keywords: Optional[dict] = None,
                 cfg: Optional[dict] = None,
                 has_creation_evidence: bool = False) -> ClaimReport:
    """核对回复里的完成态声明与本轮真实执行结果。

    - `device_meta`: {device_id: {"name","category","room"}}（由调用方注入，
      本模块不 import 设备层）
    - `has_creation_evidence`: 本轮是否真的发生了一次「草稿确认/规则落库」
    """
    if cfg is None or category_keywords is None:
        _claim_cfg, _categories = _load_cfg()
        if cfg is None:
            cfg = _claim_cfg
        if category_keywords is None:
            category_keywords = _categories

    report = ClaimReport()
    text = reply or ""
    by_device = _results_by_device(results)
    action_ids = {str(a.get("device_id") or "") for a in (actions or []) if isinstance(a, dict)}
    touched = set(by_device) | action_ids

    for m in _DONE_RE.finditer(text):
        verb = m.group("v1") or m.group("v2") or m.group("v3") or ""
        claim = m.group(0).strip()
        report.claims.append(claim)
        report.checked += 1

        ctx = _context(text, m.start(), m.end())
        # 整句包含将来时标记的，不判为"已完成"（"已关灯，稍后开空调"里第二半是计划）
        if _FUTURE_RE.search(claim):
            report.future_claims.append(claim)
            continue

        ids = _match_devices(ctx, device_meta, category_keywords, touched)
        if not ids:
            continue        # 没提到任何设备：闲聊，不判

        opposite = _VERB_COMMANDS.get(_OPPOSITE_VERB.get(verb, ""), set())
        for did in sorted(ids):
            name = (device_meta.get(did) or {}).get("name") or did
            rows = by_device.get(did) or []
            if not rows and did not in action_ids:
                report.issues.append(ClaimIssue(
                    "untouched_device", claim,
                    f"声称「{claim}」，但本轮没有对「{name}」执行任何动作", did,
                    _SEVERITY["untouched_device"]))
                continue
            if rows and not any(r.get("ok") for r in rows):
                err = next((r.get("error") for r in rows if r.get("error")), "执行失败")
                report.issues.append(ClaimIssue(
                    "failed_but_claimed", claim,
                    f"「{name}」执行失败（{err}），回复却称「{claim}」", did,
                    _SEVERITY["failed_but_claimed"]))
                continue
            if rows and opposite and all(r.get("command") in opposite for r in rows):
                cmds = "、".join(str(r.get("command")) for r in rows)
                report.issues.append(ClaimIssue(
                    "command_mismatch", claim,
                    f"「{name}」本轮执行的是 {cmds}，与「{verb}」方向相反", did,
                    _SEVERITY["command_mismatch"]))
                continue
            if rows and all(r.get("status") == "delivered" for r in rows):
                rb = (rows[0].get("readback") or {}).get("reason") or "回读未核实"
                report.issues.append(ClaimIssue(
                    "unverified", claim,
                    f"「{name}」指令已下发但{('：' + rb) if rb else '状态未经核实'}", did,
                    _SEVERITY["unverified"]))

    # 结构性写入：没有草稿确认事件却声称"已创建规则/场景"
    if not has_creation_evidence:
        for m in _CREATE_RE.finditer(text):
            claim = m.group(0).strip()
            report.issues.append(ClaimIssue(
                "creation_without_evidence", claim,
                f"回复称「{claim}」，但本轮没有确认过任何草稿或写入", "",
                _SEVERITY["creation_without_evidence"]))
    return report


def apply_suffix(reply: str, report: ClaimReport, cfg: Optional[dict] = None) -> str:
    """按配置给回复追加一句如实的注记（rewrite_reply=false 时原样返回）。"""
    cfg = cfg or {}
    if not report.issues or not cfg.get("rewrite_reply", True):
        return reply
    template = cfg.get("suffix_template") or "（提示：{detail}。请以设备实际状态为准。）"
    try:
        suffix = template.format(detail=report.detail())
    except Exception:
        suffix = "（提示：回复中的部分说法与执行记录不符，请以设备实际状态为准。）"
    return (reply or "") + suffix
