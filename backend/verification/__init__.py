# -*- coding: utf-8 -*-
"""输出层校验包。

目前只有 `claim_checker`（防"嘴上说做完了"）。它与 `perception/`、`memory/`
同属「只读世界」的层：不得执行设备、不得签发租约，`tests_agents.py` 会静态
扫描本目录守门。
"""
from .claim_checker import (ClaimIssue, ClaimReport, apply_suffix, check_claims,
                            load_claim_settings)

__all__ = ["ClaimIssue", "ClaimReport", "check_claims", "apply_suffix",
           "load_claim_settings"]
