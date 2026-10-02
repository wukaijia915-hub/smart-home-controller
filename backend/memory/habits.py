# -*- coding: utf-8 -*-
"""记忆源二：用户习惯统计 + 显式偏好一致性校验。

两件事：
1. **统计**：用户操作了哪些设备/场景、参数偏好值分布。用于在 prompt 里给模型
   一句「用户习惯参考」，以及给日报提供数据。
2. **一致性校验（「交叉验证」思路的第二次落地）**：用户**显式说过**的偏好
   （"我喜欢 26 度"）与系统**观察到**的实际行为（他其实总在调到 24 度）冲突时，
   主动澄清，而不是二选一地默默猜。
   —— 心镜的对应物是「AI 判断 vs 量表自评」交叉校验（`/api/scales/crosscheck`）。
   量表是心理测量工具，家居场景无对应物，但**这个机制**是通用的，换数据源即可。

存储：`data/user_habits.json`（运行时状态，已在 .gitignore）。
旧版把习惯存在 `backend/knowledge/user_habits.json`；首次运行时若旧文件存在而
新文件不存在，会**读入旧数据再落到新路径**，不丢用户已积累的记录。
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from ..config_loader import DEFAULT_DATA_DIR, data_path

HABITS_PATH = data_path("user_habits.json")
# 迁移源只认**规范位置**，不认被 `LINGXI_DATA_DIR` 覆盖后的位置。
# 之前这里比较的是 `self.path == HABITS_PATH` —— 而 HABITS_PATH 本身就是
# 带覆盖读出来的，于是判据恒真，测试临时目录照样会导入生产数据。
CANONICAL_PATH = DEFAULT_DATA_DIR / "user_habits.json"
LEGACY_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "user_habits.json"

EMPTY = {"commands": {}, "scenes": {}, "preferences": {}, "declared": {}}

# 显式偏好语句：「我喜欢/我习惯/以后都/默认 + 数字 + 单位」
_DECLARED = re.compile(
    r"(?:我喜欢|我习惯|我偏好|我倾向于|以后都|以后|默认|记住)[^0-9]{0,6}(\d{1,3})\s*(度|℃|%|百分)"
)
_UNIT_KEY = {"度": "temperature", "℃": "temperature", "%": "brightness", "百分": "brightness"}

# 观察到的偏好（设备属性名 → 偏好键），与 _UNIT_KEY 的取值域对齐
_OBSERVED_ATTR = {"temperature": ("set_temperature", "temperature"),
                  "brightness": ("set_brightness", "brightness")}

# 触发澄清的最小观察次数：样本太少就下结论等于用噪声下结论
MIN_OBSERVATIONS = 3
MIN_DIVERGENCE = 1.0
# 观察结果按设备分别统计。**不能跨设备取全局众数**：客厅空调长期 24℃、
# 卧室空调长期 26℃ 时，全局众数是哪一个取决于两边样本数，会把两台设备各自的
# 稳定偏好抹平成"看起来两者一致"，冲突检测随之失效（实测踩到过）。
# 用户声明的偏好是全局的，冲突判定则必须落到具体设备上才有意义。


class HabitStore:
    def __init__(self, path: Path | None = None):
        self.path = path or HABITS_PATH
        self._lock = threading.Lock()
        self.data = self._load()

    # ---------- 载入 / 落盘 ----------
    def _load(self) -> dict:
        # 旧路径迁移**只在规范数据目录下发生**。指向临时目录（测试）时不做迁移，
        # 否则每次测试都会把生产环境累积的历史数据搬进临时库，
        # 断言依赖的"观察次数"就变成不可复现的（已实测：迁移过来的 20 条历史
        # 样本会盖住本次测试写入的 3 条，"众数"因此指向错误的值）。
        candidates = [self.path]
        if self.path == CANONICAL_PATH:
            candidates.append(LEGACY_PATH)
        for p in candidates:
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                merged = dict(EMPTY)
                merged.update(data or {})
                for k, v in EMPTY.items():
                    merged.setdefault(k, dict(v))
                if p is LEGACY_PATH:
                    print(f"[记忆·习惯] 从旧路径迁入 {p.name} → {self.path}")
                return merged
            except Exception as e:
                print(f"[记忆·习惯] 加载失败（{p}）: {e}")
        return {k: dict(v) for k, v in EMPTY.items()}

    def _save(self) -> None:
        """原子落盘：先写临时文件再 replace。

        这个方法会被 `asyncio.to_thread` 的工作线程调用。直接覆写目标文件时，
        进程若在写到一半时退出（演示现场关窗口、Ctrl-C），文件会变成截断的
        半个 JSON —— 下次启动习惯数据全部丢失，且恢复不了。
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[记忆·习惯] 保存失败: {e}")

    # ---------- 写入 ----------
    def record_command(self, device_id: str, command: str, params: dict) -> None:
        if not device_id or not command:
            return
        with self._lock:
            key = f"{device_id}:{command}"
            self.data["commands"][key] = self.data["commands"].get(key, 0) + 1
            if params:
                pref_key = f"{device_id}:{command}:params"
                series = self.data["preferences"].setdefault(pref_key, [])
                series.append(params)
                del series[:-20]        # 只保留最近 20 条，避免文件无限增长
            self._save()

    def record_scene(self, scene: str) -> None:
        if not scene:
            return
        with self._lock:
            self.data["scenes"][scene] = self.data["scenes"].get(scene, 0) + 1
            self._save()

    def record_declared(self, text: str) -> Optional[dict]:
        """从用户话语里抽取**显式声明的偏好**。抽不到返回 None。

        只在用户明确表达偏好时记录（"我喜欢 26 度"），不从普通控制指令里推断 ——
        "空调调到 24 度" 是这一次的指令，不是长期偏好，混在一起会让后面的
        一致性校验变成自问自答。
        """
        m = _DECLARED.search(text or "")
        if not m:
            return None
        pref_key = _UNIT_KEY.get(m.group(2))
        if not pref_key:
            return None
        entry = {"value": float(m.group(1)), "raw": m.group(0), "at": _now()}
        with self._lock:
            self.data["declared"][pref_key] = entry
            self._save()
        return {"preference": pref_key, **entry}

    def clear_declared(self, pref_key: str) -> bool:
        with self._lock:
            if pref_key in self.data["declared"]:
                del self.data["declared"][pref_key]
                self._save()
                return True
        return False

    # ---------- 读取 ----------
    def _observed_by_device(self, pref_key: str) -> dict[str, dict]:
        """按设备统计观察到的偏好：{device_id: {mode, observations}}。

        只统计样本数达到 MIN_OBSERVATIONS 的设备 —— 某台设备只被调过一次，
        那一次的值不足以称为「偏好」。
        """
        pair = _OBSERVED_ATTR.get(pref_key)
        if not pair:
            return {}
        suffix = f":{pair[0]}:params"
        per_device: dict[str, list[float]] = {}
        for key, series in self.data.get("preferences", {}).items():
            if not key.endswith(suffix):
                continue
            device_id = key[: -len(suffix)]
            for params in series:
                v = params.get(pair[1]) if isinstance(params, dict) else None
                if isinstance(v, (int, float)):
                    per_device.setdefault(device_id, []).append(float(v))
        out = {}
        for device_id, values in per_device.items():
            mode, count = Counter(values).most_common(1)[0]
            if count >= MIN_OBSERVATIONS:
                out[device_id] = {"mode": mode, "observations": count,
                                  "samples": len(values)}
        return out

    def consistency_check(self, name_of: Optional[Callable[[str], str]] = None) -> list[dict]:
        """显式声明 vs 实际行为的一致性校验。返回冲突清单（无冲突为空）。

        `name_of` 是可选的 device_id → 显示名映射。记忆层不导入设备注册表
        （那会让它有能力读世界），所以显示名由调用方注入；不注入时退回 device_id。
        """
        conflicts = []
        for pref_key, declared in self.data.get("declared", {}).items():
            declared_value = float(declared.get("value", 0))
            for device_id, obs in self._observed_by_device(pref_key).items():
                gap = abs(obs["mode"] - declared_value)
                if gap < MIN_DIVERGENCE:
                    continue
                conflicts.append({
                    "preference": pref_key,
                    "device_id": device_id,
                    "device_name": name_of(device_id) if name_of else device_id,
                    "declared": declared_value,
                    "observed": obs["mode"],
                    "observations": obs["observations"],
                    "divergence": round(gap, 2),
                    # 问句，不是动作 —— 与感知层同一原则：不确定的事只问，不做
                    "question": self._clarify_text(
                        pref_key, declared_value, obs["mode"], obs["observations"],
                        name_of(device_id) if name_of else device_id),
                })
        conflicts.sort(key=lambda c: c["divergence"], reverse=True)
        return conflicts

    @staticmethod
    def _clarify_text(pref_key: str, declared, observed, count: int, where: str) -> str:
        label = {"temperature": "温度", "brightness": "亮度"}.get(pref_key, pref_key)
        unit = "℃" if pref_key == "temperature" else "%"
        return (f"您之前说过偏好 {declared:g}{unit}，但我注意到「{where}」最近 {count} 次"
                f"都是调到 {observed:g}{unit}。要把偏好更新为 {observed:g}{unit} 吗？")

    def hint(self, query: str = "") -> str:
        """给 prompt 用的一句话习惯参考；数据不足时返回空串（不编造）。"""
        hints = []
        scenes = self.data.get("scenes", {})
        if scenes:
            top_scene, n = Counter(scenes).most_common(1)[0]
            if n >= 3:
                hints.append(f"用户最常用场景是「{top_scene}」(使用 {n} 次)")
        commands = self.data.get("commands", {})
        if commands:
            top_cmd, n = Counter(commands).most_common(1)[0]
            if n >= 3:
                device, _, cmd = top_cmd.partition(":")
                hints.append(f"用户频繁操作「{device}」的「{cmd}」({n} 次)")
        for pref_key, declared in self.data.get("declared", {}).items():
            unit = "℃" if pref_key == "temperature" else "%"
            label = {"temperature": "温度", "brightness": "亮度"}.get(pref_key, pref_key)
            hints.append(f"用户明确表示偏好{label} {declared.get('value'):g}{unit}")
        return "；".join(hints)

    def stats(self, name_of: Optional[Callable[[str], str]] = None) -> dict:
        preferences = self.data.get("preferences", {})
        observed = {k: self._observed_by_device(k) for k in _OBSERVED_ATTR}
        return {
            "total_commands": sum(self.data.get("commands", {}).values()),
            "total_scenes": sum(self.data.get("scenes", {}).values()),
            "top_scene": (Counter(self.data.get("scenes", {})).most_common(1) or [(None, 0)])[0][0],
            "top_commands": Counter(self.data.get("commands", {})).most_common(5),
            "preference_samples": sum(len(s) for s in preferences.values()),
            "observed_preferences": observed,
            "declared_preferences": self.data.get("declared", {}),
            "conflicts": self.consistency_check(name_of=name_of),
        }


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


habits = HabitStore()
