# -*- coding: utf-8 -*-
"""Gate —— 统一的「门控」抽象。

## 为什么要有这个抽象

本项目有两条各自独立的"不信任单一信号源"的防线，它们在**结构上是同一件事**：

| 层 | 单一信号源的风险 | 门控机制 |
|---|---|---|
| 感知层 | 单个模态误判（光照导致面部误判、传感器抖动） | 多模态置信度自适应加权 + 模态一致性复核，冲突即丢弃 |
| 执行层 | LLM 幻觉 / 越权 / 重放 | 授权租约 + 状态版本校验 + 高危二次确认 |

两者都是「默认不信任 → 必须交叉验证 → 验证不过就拒绝，而不是降级使用」。
把它们收敛到同一个接口，好处不只是少写代码：

1. 门控的失败处理逻辑可以统一（记录 evidence → 降级 → 通知），不必写两遍
2. **"感知层管住信号、执行层管住模型"这句话在代码里有同一处落点**，
   而不是靠文档里的类比 —— 答辩时可以直接指着这个文件说
3. 新增任何门控（如未来的工具权限门控）时有既定的落点，不会各写各的

## 约定

- `passed=False` 表示**拒绝**，调用方不得"降级使用"该结果，只能走兜底
- `evidence` 必须能解释"为什么拒绝"，它是可解释性的载体，不是调试信息
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GateResult:
    """一次门控评估的结果。"""
    passed: bool
    confidence: float = 0.0
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "evidence": self.evidence,
        }


class Gate(ABC):
    """门控抽象。实现类必须能回答：这个输入该不该被采纳，以及为什么。"""

    name: str = "gate"

    @abstractmethod
    def evaluate(self, subject: Any) -> GateResult:
        """评估一个输入。返回 passed=False 时，调用方必须拒绝而非降级使用。"""
        raise NotImplementedError
