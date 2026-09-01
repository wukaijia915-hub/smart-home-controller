# -*- coding: utf-8 -*-
"""JSON 配置加载器：设备/场景/环境从 config/ 目录的 JSON 文件加载。
支持热重载（修改 JSON 后调用 reload() 即可生效，无需改代码）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parent / "config"


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


def reload_all() -> dict:
    """重新加载所有配置（热重载）。"""
    return {
        "devices": load_devices(),
        "scenes": load_scenes(),
        "environment": load_environment(),
    }
