# -*- coding: utf-8 -*-
"""算力后端探测与适配（移植自心镜 `backend/vibraimage/gpu_backend.py`，Apache-2.0）。

## ⚠️ 关于"国产算力"这个说法，先说明白

心镜的宣传口径是「MUSA → CUDA → numpy 三级自动探测链，零 NVIDIA 依赖」。
读源码后可以确认：**它并没有独立的 MUSA 代码路径**。实现方式是
「`torch.cuda.is_available()` + 对设备名做 `metax`/`musa` 子串匹配来识别厂商」，
即依赖沐曦 PyTorch 预装环境暴露 CUDA 兼容接口。所以真实结构是**两级**：

    torch（加速器，可能是 NVIDIA 也可能是沐曦等 CUDA 兼容实现） → numpy（CPU 兜底）

本文件按**实际结构**移植，厂商识别如实标注为"设备名启发式"，
不把它包装成三级链，也不声称"零 NVIDIA 依赖"（用了 torch 就依赖其后端实现）。

## ⚠️ 当前没有热路径 —— 这一点必须写在这里

灵犀感知层目前**没有任何计算密集的模态**：视觉模态是关键词规则模拟，
环境行为模态是少量标量运算。**所以本模块当前不加速任何东西**，
它的价值是为将来接入真实计算负载留好接缝（例如真正接入摄像头帧处理、
或接入前庭引擎的帧差分/FFT）。

**因此不要在当前阶段把"国产算力适配"写成申报书的创新点或加分项** ——
没有真实负载与基准数据支撑，声称了也答不出"加速比多少、加速了什么"。
等有了真实负载，用 `benchmark()` 拿到数据再写。

## 用法

    from backend.perception.gpu import get_array_module, to_device, to_cpu
    xp = get_array_module()          # torch 或 numpy
    result = xp.abs(xp.diff(frames, axis=0))

环境变量：
    LINGXI_GPU_BACKEND=auto|torch|numpy     （默认 auto）
    LINGXI_GPU_BENCHMARK=1                  开启后记录 CPU/加速器耗时对比
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("perception.gpu")

# 设备名 → 厂商的启发式匹配表。这是**子串匹配的猜测**，不是可靠的厂商 API 查询；
# 未命中时如实标为 "unknown" 而不是硬塞一个名字。
_VENDOR_HINTS = [
    ("metax", "MetaX (沐曦)"),
    ("musa", "MetaX (沐曦)"),
    ("nvidia", "NVIDIA"),
    ("geforce", "NVIDIA"),
    ("tesla", "NVIDIA"),
    ("ascend", "Huawei Ascend (昇腾)"),
    ("huawei", "Huawei Ascend (昇腾)"),
]

_state: dict = {"module": None, "info": None}
_benchmark_enabled = os.getenv("LINGXI_GPU_BENCHMARK", "").strip().lower() in ("1", "true")
_benchmark_records: list = []


def _backend_preference() -> str:
    return os.getenv("LINGXI_GPU_BACKEND", "auto").strip().lower()


def _guess_vendor(device_name: str) -> str:
    lowered = (device_name or "").lower()
    for hint, vendor in _VENDOR_HINTS:
        if hint in lowered:
            return vendor
    return "unknown"


def detect_accelerator() -> dict:
    """探测可用的加速器。结果会被缓存。

    返回字段中的 `vendor` 是**设备名启发式推断**，不可作为硬性判断依据。
    """
    if _state["info"] is not None:
        return _state["info"]

    info = {"available": False, "backend": "numpy", "device": "cpu",
            "vendor": "cpu", "model": "CPU (numpy)", "memory_mb": 0,
            "vendor_detection": "n/a"}

    if _backend_preference() in ("auto", "torch"):
        try:
            import torch
            if torch.cuda.is_available():
                model = torch.cuda.get_device_name(0)
                try:
                    memory_mb = int(
                        torch.cuda.get_device_properties(0).total_memory / 1024 / 1024)
                except Exception:
                    memory_mb = 0
                info.update({
                    "available": True, "backend": "torch", "device": "cuda:0",
                    "model": model, "memory_mb": memory_mb,
                    "vendor": _guess_vendor(model),
                    "vendor_detection": "device-name substring heuristic",
                })
                logger.info(f"加速器: {info['vendor']} {model}, {memory_mb}MB")
                _state["info"] = info
                return info
        except ImportError:
            logger.info("未安装 torch，回退 CPU")
        except Exception as e:
            logger.warning(f"torch 探测异常，回退 CPU: {type(e).__name__}: {e}")

    _state["info"] = info
    return info


def get_array_module():
    """返回当前活动的数组模块（torch 或 numpy）。

    与 numpy 的 API 兼容是调用方需要自己保证的 —— 只使用两者共有的算子
    （`abs`/`diff`/`mean`/`std` 等），不要用某一方独有的接口。
    """
    if _state["module"] is not None:
        return _state["module"]

    if _backend_preference() != "numpy" and detect_accelerator()["available"]:
        import torch
        _state["module"] = torch
        return torch

    import numpy
    _state["module"] = numpy
    return numpy


def accelerator_available() -> bool:
    return detect_accelerator()["available"]


def to_device(array):
    """把数组搬到加速器（可用时），否则原样返回。"""
    if not accelerator_available():
        return array
    import torch
    if isinstance(array, torch.Tensor):
        return array.cuda()
    return torch.from_numpy(array).cuda()


def to_cpu(array):
    """把数组搬回 CPU 的 numpy 表示。"""
    if array is None:
        return None
    try:
        import torch
        if isinstance(array, torch.Tensor):
            return array.detach().cpu().numpy()
    except ImportError:
        pass
    return array


def reset() -> None:
    """清空缓存（测试用）。"""
    _state["module"] = None
    _state["info"] = None


# ---------------------------------------------------------------- 基准
@contextmanager
def benchmark(label: str):
    """记录一段计算的耗时与所用后端，供 `benchmark_records()` 汇总。

    只在 `LINGXI_GPU_BENCHMARK=1` 时生效，避免常态运行时的额外开销。
    """
    if not _benchmark_enabled:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _benchmark_records.append({
            "label": label,
            "backend": detect_accelerator()["backend"],
            "device": detect_accelerator()["device"],
            "duration_ms": round((time.perf_counter() - t0) * 1000, 3),
        })


def benchmark_records() -> list:
    return list(_benchmark_records)


def status() -> dict:
    info = dict(detect_accelerator())
    info["preference"] = _backend_preference()
    info["benchmark_enabled"] = _benchmark_enabled
    info["has_hot_path"] = False
    info["note"] = ("感知层当前无可加速的计算负载，本模块仅提供接缝；"
                    "在接入真实计算密集模态前，不得将其作为性能或国产化成果主张")
    return info
