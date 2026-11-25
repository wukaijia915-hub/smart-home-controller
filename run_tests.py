#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一回归入口 —— 一次跑完 13 个测试脚本，并替你把环境隔离好。

## 为什么不直接 `python tests_xxx.py` 手敲

三个坑都实际踩过（见 CLAUDE.md 5.4）：

1. **数据污染**：不设 `LINGXI_DATA_DIR` 时，测试会写真实的 `data/`，
   累积出「用户最常用场景 home（使用 1842 次）」这种纯噪声结论。
2. **非确定性**：不设 `LLM_OFFLINE=1` 时，测试会打真实模型接口，
   结果随模型输出波动（曾因返回 JSON 数组导致 manager 崩溃）。
3. **编码**：Windows 控制台默认 GBK，打印 ✓ 会 UnicodeEncodeError，
   整批测试"看起来红了"其实全过。设置 PYTHONIOENCODING=utf-8 才能复现真实结果。

用法：
    python run_tests.py                 # 全量门禁（13 个脚本）
    python run_tests.py --verbose       # 打印每个脚本完整输出
    python run_tests.py --only tests_agents.py tests_loops.py
    python run_tests.py --no-isolate    # 复用当前 LINGXI_DATA_DIR（调试用）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 自己的 stdout 也要 UTF-8：子进程里设 PYTHONIOENCODING 不会影响本进程，
# 而本进程要打印 ✓ / ✅ / 中文 —— Windows 默认 GBK 控制台会直接
# UnicodeEncodeError，让"13/13 通过"在最后一行崩掉（实测踩过）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 顺序即门禁顺序：从设备/决策基础，到护栏、感知、记忆、观测、双环、性能、CSI。
# 新增脚本请加在这里，并同步更新 CLAUDE.md / README 的"13 个"计数。
SCRIPTS = [
    "tests_smoke.py",
    "tests_agents.py",
    "tests_capabilities.py",
    "tests_edge_cloud.py",
    "tests_error.py",
    "tests_funnel.py",
    "tests_budget.py",
    "tests_perception.py",
    "tests_memory.py",
    "tests_observability.py",
    "tests_loops.py",
    "tests_performance.py",
    "tests_csi.py",
    "tests_trust.py",
    "tests_adaptation.py",
]

TIMEOUT_SECONDS = 600


def _run(script: str, env: dict, verbose: bool) -> tuple[bool, float, str]:
    path = ROOT / script
    if not path.exists():
        return False, 0.0, f"文件不存在: {script}"
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, script],
        cwd=str(ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=TIMEOUT_SECONDS,
    )
    elapsed = time.perf_counter() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    if verbose:
        print(output)
    return proc.returncode == 0, elapsed, output


def main() -> int:
    parser = argparse.ArgumentParser(description="灵犀统一回归入口")
    parser.add_argument("--only", nargs="*", default=None, help="只跑指定脚本")
    parser.add_argument("--verbose", action="store_true", help="打印完整输出")
    parser.add_argument("--no-isolate", action="store_true",
                        help="不创建临时 LINGXI_DATA_DIR（调试用，会写真实 data/）")
    parser.add_argument("--list", action="store_true", help="只列出脚本")
    args = parser.parse_args()

    if args.list:
        for s in SCRIPTS:
            print(s)
        return 0

    scripts = args.only or SCRIPTS
    unknown = [s for s in scripts if s not in SCRIPTS]
    if unknown:
        print(f"未知脚本: {', '.join(unknown)}（用 --list 查看）")
        return 2

    env = dict(os.environ)
    env["LLM_OFFLINE"] = "1"           # 强制离线：回归只测我们自己的代码
    env["PYTHONIOENCODING"] = "utf-8"  # 中文 + ✓ 在 GBK 控制台会炸
    env["PYTHONUTF8"] = "1"

    tmp_dir = None
    if not args.no_isolate:
        tmp_dir = tempfile.mkdtemp(prefix="lingxi_tests_")
        env["LINGXI_DATA_DIR"] = tmp_dir   # 与真实用户数据隔离

    print("=" * 64)
    print(f"  犀鉴回归门禁：{len(scripts)} 个脚本"
          f"（LLM_OFFLINE=1，数据目录={'临时' if tmp_dir else '当前'}）")
    print("=" * 64)

    failures = []
    results = []
    try:
        for script in scripts:
            ok, elapsed, output = _run(script, env, args.verbose)
            results.append((script, ok, elapsed))
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {script:<28} {elapsed:6.2f}s")
            if not ok:
                failures.append(script)
                tail = "\n".join(output.strip().splitlines()[-12:])
                print("         --- 末尾输出 ---")
                for line in tail.splitlines():
                    print(f"         {line}")
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    total = len(results)
    passed = total - len(failures)
    print("-" * 64)
    print(f"  结果：{passed}/{total} 通过"
          + (f"，失败：{', '.join(failures)}" if failures else " ✅"))
    print("=" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
