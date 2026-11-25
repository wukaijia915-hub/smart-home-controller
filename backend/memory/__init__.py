# -*- coding: utf-8 -*-
"""记忆层三源。

| 源 | 文件 | 内容 | 变化速度 |
|---|---|---|---|
| 手册 | `manuals.py` | 设备说明书（静态知识，随仓库提交） | 几乎不变 |
| 习惯 | `habits.py` | 用户设备/场景使用统计 + 显式声明的偏好 | 每次操作 |
| 经验 | `experience.py` | 成功规划案例（慢环 prompt 注入） | 每次慢环 |

**硬边界：记忆层只读世界，不写世界。** 三个源都不导入 `DeviceRegistry` /
`lease`，不产生任何设备动作。它们只能**影响 prompt**（进而影响 LLM 的提议），
而提议仍要过 Manager 的校验闸门。`tests_memory.py` 用静态扫描把这条钉住 ——
理由与感知层相同：这条承诺靠代码评审守不住。

**运行时状态一律落在 `data/`**（已在 .gitignore）。`backend/memory/` 只放代码。
"""
from . import habits, manuals, experience  # noqa: F401

__all__ = ["manuals", "habits", "experience"]
