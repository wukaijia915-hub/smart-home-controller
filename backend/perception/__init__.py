# -*- coding: utf-8 -*-
"""感知层（L2）。

把「视觉行为 / 面部表情 / 环境行为 / 前庭振动(可选)」多模态读数融合为一个
可解释、经一致性复核的情境判断，供数字人生成**询问式建议**。

## 两条不可违背的边界

### 1. 感知结果绝不进入控制回路

    PERCEPTION_CANNOT_ACTUATE = True

融合结果只能生成询问式话术（"看起来有点累，要不要把灯调暖一点？"），
**绝不自动执行设备动作**。原因有两层：

- 工程上：未经充分验证的感知信号不该直接驱动物理世界动作
- 诚实性上：心镜自己的评测数据显示其前庭引擎 14 个参数修复后仅 2/14 ANOVA 显著、
  稳定性 ICC 多为负值；用户自己的论文实验显示微运动特征对效价 r=0.021（≈无信号）。
  把它当作控制回路的输入等于把一个已被证伪的测量挂在系统最显眼处

我们接入它、用模态一致性检验管住它、并如实报告它的边界 —— 而不是不使用它，
也不是假装它可靠。`tests_perception.py` 会断言这条边界。

### 2. 原始画面/音频不出户

所有模态在端侧完成推理，只把**结构化标签**交给决策层。复用 `edge_cloud` 的
L0 分级：视觉/音频原始数据属最高敏，离开本机的一律只有标签。
每个 `ModalReading` 的 `local_only` 字段记录这一点。

## 模块

- `fusion.py`      多模态置信度融合（移植心镜算法，二模态推广到 N 模态）
- `modal_gate.py`  MultiModalGate —— 把融合结果包成统一的门控结果
- `modalities/`    各模态检测器
- `situation.py`   白盒情境参数（可解释、可验证，与融合同源但独立）
- `gpu.py`         国产算力探测链（MUSA → CUDA → numpy）
"""
from __future__ import annotations

#: 硬边界：感知结果不得产生设备动作。任何试图从感知结果直接调
#: `DeviceRegistry.control()` 或构造租约的代码都是 bug。
PERCEPTION_CANNOT_ACTUATE = True

from .fusion import (  # noqa: E402
    EMOTION_7_CLASSES,
    EMOTION_7_VA,
    MIN_FUSED_CONFIDENCE,
    MODAL_DISTANCE_THRESHOLD,
    FusionOutcome,
    ModalReading,
    MultiModalFuser,
    classify_emotion_7class,
    entropy_confidence,
    stability_confidence,
)
from .modal_gate import MultiModalGate  # noqa: E402
from .modalities import (  # noqa: E402
    EnvironmentBehaviorModality,
    FacialExpressionModality,
    ModalityDetector,
    PerceptionContext,
    VestibularModality,
    VisionBehaviorModality,
    default_modalities,
)
from .service import PerceptionResult, PerceptionService  # noqa: E402
from .situation import (  # noqa: E402
    PresenceTracker,
    SituationAssessor,
    SituationInput,
    SituationParam,
    SituationState,
)

__all__ = [
    "PERCEPTION_CANNOT_ACTUATE",
    "EMOTION_7_CLASSES",
    "EMOTION_7_VA",
    "MIN_FUSED_CONFIDENCE",
    "MODAL_DISTANCE_THRESHOLD",
    "FusionOutcome",
    "ModalReading",
    "MultiModalFuser",
    "MultiModalGate",
    "classify_emotion_7class",
    "entropy_confidence",
    "stability_confidence",
    # 模态
    "ModalityDetector",
    "PerceptionContext",
    "VisionBehaviorModality",
    "FacialExpressionModality",
    "EnvironmentBehaviorModality",
    "VestibularModality",
    "default_modalities",
    # 服务
    "PerceptionService",
    "PerceptionResult",
    # 白盒情境参数
    "SituationAssessor",
    "SituationInput",
    "SituationParam",
    "SituationState",
    "PresenceTracker",
]
