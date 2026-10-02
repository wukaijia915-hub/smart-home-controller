# -*- coding: utf-8 -*-
"""JSON 配置加载器：设备/场景/环境从 config/ 目录的 JSON 文件加载。
支持热重载（修改 JSON 后调用 reload() 即可生效，无需改代码）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parent / "config"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def data_dir() -> Path:
    """运行时状态目录（习惯 / 经验 / 日报 / 定时任务）。

    可用环境变量 `LINGXI_DATA_DIR` 覆盖。存在的理由不是「可配置」本身，
    而是**把回归测试与真实用户数据隔开**：此前测试直接写
    `backend/knowledge/user_habits.json`，累积几千次测试调用后，
    界面上会显示「用户最常用场景 home（使用 1842 次）」这种纯属测试噪声的结论。
    测试改成指向临时目录后，真实数据才可信。
    """
    override = os.getenv("LINGXI_DATA_DIR", "").strip()
    return Path(override) if override else DEFAULT_DATA_DIR


def data_path(name: str) -> Path:
    return data_dir() / name


def _load_json(filename: str) -> dict[str, Any]:
    path = CONFIG_DIR / filename
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_devices() -> list[dict]:
    """加载设备配置清单。"""
    data = _load_json("devices.json")
    return data.get("devices", [])


def load_scenes() -> dict[str, dict]:
    """加载场景模式配置。"""
    data = _load_json("scenes.json")
    return data.get("scenes", {})


def load_environment() -> dict:
    """加载环境传感器仿真配置。"""
    return _load_json("environment.json")


def load_loops() -> dict:
    """加载快慢双环参数（内环周期 / 外环日报时刻）。

    这些数值不写死在 Python 里：演示时改 `config/loops.json` 就能把日报
    从 22:00 挪到当前分钟，不必改代码重跑。
    """
    return _load_json("loops.json")


def load_alert_routes() -> dict:
    """加载内环告警阈值配置（声明式，替代 main.py 里成排的 if）。"""
    return _load_json("alert_routes.json")


def reload_all() -> dict:
    """重新加载所有配置（热重载）。"""
    return {
        "devices": load_devices(),
        "scenes": load_scenes(),
        "environment": load_environment(),
    }
