# 为什么不用 LangChain / LangGraph

> 技术选型论证。本文档回答一个答辩必问的问题：
> **"这和直接用 LangChain Agent 有什么区别？"**
>
> 结论先行：**不是"LangChain 不好"，而是它解决的问题不是我们的瓶颈，而它带来的
> 故障面恰好落在我们最不想出问题的地方。**
>
> 本文所有关于心镜的论断都可在其源码中核对（`Xinjing-AIGC-Agent-Platform/backend/`）。

---

## 一、先明确：LangChain 能给我们什么

诚实地说，它确实能提供三样东西：

| 能力 | 我们的替代 |
|---|---|
| 工具调用的统一抽象（`@tool` 装饰器、schema 生成） | `backend/toolkit/schema_gen.py` —— **从 `Device.attributes` 反推 schema** |
| ReAct / Agent 循环 | 自研约 80 行（**实测后否决了这条路线**，见 §四） |
| 多模型适配（OpenAI / Anthropic / 本地） | `backend/llm_client.py` —— 4 个预设后端 + 熔断 + 复杂度路由 |

**它不是没有价值，而是它的价值在本项目里可以由更小的代价获得。**

---

## 二、依赖面（最直接的一条）

### 灵犀的依赖清单（全部）

```
fastapi>=0.110
uvicorn[standard]>=0.29
requests>=2.31
python-dotenv>=1.0
edge-tts>=7.0
apscheduler>=3.10
```

**6 行。**

### 心镜的依赖清单（摘录）

```
langchain>=0.1.0          langgraph>=0.0.30       langchain-openai>=0.1.0
sqlalchemy>=2.0.0         pydantic-settings>=2.0.0
opencv-python-headless>=4.5.0   numpy>=1.21.0    scipy>=1.7.0
ultralytics>=8.0.0        torch>=2.0.0           deepface>=0.0.90
tensorflow>=2.15.0        matplotlib>=3.3.0       pyyaml>=6.0
```

心智负担不在"多装几个包"，而在**这几行之间的版本组合**：`langchain` 与
`langchain-core` 与 `langgraph` 三者的版本兼容矩阵，是社区长期抱怨的痛点；
`tensorflow` + `torch` 装在同一个环境里是另一个经典坑。

**灵犀的核心卖点是「零硬件、pip install 即跑、浏览器打开就能演示」。**
引入 LangChain 会让这个卖点变成"取决于你的环境能不能装成功"。

> 本项目对重依赖的处理方式已经写进 `CLAUDE.md`：`requirements.txt` 保持 6 行；
> 重型依赖（如 TensorFlow）必须放 `requirements-vision.txt`，
> **并保证未安装时优雅降级**。感知层的面部/前庭模态就是这么做的。
> LangChain 不属于"可降级"那一类 —— 它一旦不在，Agent 主链路就没了。

---

## 三、护栏在架构里，不在框架里（最关键的一条）

这是本项目与"直接用 LangChain Agent"最本质的区别。

**LangChain Agent 的安全模型是**：把工具绑定给模型，让模型自己选。护栏（如果有）
体现在 **prompt 里的约束**和**工具的粒度设计**上。

**灵犀的安全模型是**：模型选错设备或越界参数，结果是**被拒绝**，不是**被执行**。

具体地：

| 机制 | 落点 | 效果 |
|---|---|---|
| Root 的工具视图**全部只读** | `agents/root_agent.py` | 提议阶段物理上无法影响世界 |
| 每个副作用动作强制走租约链 | `toolkit/guard.py::ManagerGuard` | 无租约则拒绝执行（fail closed） |
| 高危动作二次确认 | `lease.py::HIGH_RISK` | `lock.unlock` / `gas_stove.turn_on` 等 4 类必须人工确认 |
| 参数包络 | 阶段 2（**未完成，缺陷 D7**） | —— |
| 状态版本校验 | `state.py` + `lease.py` | 防重放与陈旧计划 |
| 回读三态证据 | `devices/registry.py` | `confirmed` / `delivered` / `failed` 三段式 |

**这套东西 LangChain 给不了**，因为它不是框架层的能力，而是**领域架构的产物**：
它来自"家居设备的动作有物理后果、有不可逆操作、有安全等级"这个领域事实。

> **顺带说一个真实教训**：本项目上线后发现 `/api/tools/call` 与 MCP `tools/call`
> 直接调 `registry.control()`，**完全绕过了上述护栏** —— 实测可解锁门锁、点燃燃气灶
> （缺陷 D22）。修法**不是**再写一套校验，而是把工具调用包装成单动作 `TaskPlan`
> 交给同一个 `ManagerAgent`，让两条路径共用同一条链。
>
> 这件事说明：**护栏的价值完全取决于它是不是唯一路径。** 而"是不是唯一路径"
> 恰恰是引框架时最容易失控的地方 —— 框架会自己长出一条调用通路。

---

## 四、"那我不用框架，自己写 ReAct 总行了吧？"

**这是本项目的原始方案。实测把它否决了。**

原计划的慢环是 prompt-based ReAct 工具循环（理由是心镜技术报告背书
「可复用到任何不支持 function calling 的大模型」，而灵犀接的国产模型正好属于这一类）。

四条实验数据否定了它（完整记录见 `experiments/README.md`）：

| # | 实验 | 结果 |
|---|---|---|
| 1 | ReAct 格式遵循度（15 用例） | **80%（12/15）**，失败用例 3/3 稳定复现 |
| 2 | 让 Root 直接输出具体动作的幻觉率 | **0%（0/25）** —— 原方案"会引入幻觉"的风险假设不成立 |
| 3 | 延迟归因对照实验 | **与调用次数、prompt 体积无关**，只与输出 token 数线性相关 |
| 4 | 批量翻译可行性（8 次探测） | **全畸形** |

**结论**：ReAct 循环既不可靠、也不带来延迟收益。最终实现 **Design B** ——
Root **单次输出具体动作**，Manager **不再持有 LLM**，成为纯护栏。

于是"要不要用 LangChain 的 ReAct"这个问题本身失去了意义：**我们不需要 ReAct。**

---

## 五、心镜自己的 LangGraph 用法说明了什么

心镜确实用了 `langgraph.prebuilt.create_react_agent`。核对源码后有三点值得记录：

### 5.1 它的"降级分支"其实是硬失败

`backend/agents/base_agent.py`：

```python
try:
    from langgraph.prebuilt import create_react_agent
    _LANGGRAPH_AVAILABLE = True
except ImportError:
    _LANGGRAPH_AVAILABLE = False
    create_react_agent = None
...
def build_agent(self):
    """...moark.com 等不支持的平台自动降级到 prompt-based 模式。"""
    if not _LANGGRAPH_AVAILABLE:
        raise ImportError("langgraph 未安装，请运行: pip install langgraph")
```

`_LANGGRAPH_AVAILABLE` 这个标志**看起来**是"优雅降级"，但 `build_agent()` 直接
`raise ImportError` —— **没有 langgraph 就是硬失败**，不是降级。

> 这不是要挑刺，而是一个工程判断：**引入重框架后，"没有它怎么办"这件事会被
> 推迟到一个不被测试的路径上。** 本项目要求每个可选依赖都必须有可演示的降级路径
> （感知层的面部/前庭模态就是这么做的，`requirements-vision.txt` 不装也能跑）。
> 用框架时这条纪律很难维持。

### 5.2 docstring 承诺的能力，在默认配置下不可达

docstring 说"moark.com 等不支持的平台自动降级到 prompt-based 模式"。
实际机制是 `think()` 里的 `if self.platform == "lingshu"` 分流，
而 **`platform` 默认为 `None`** —— 默认配置下这个分支根本走不到。
此外 `stream_think` 在 lingshu 分支用 `tools=[]`，**永远不吐 action/observation 事件**。

**因此"心镜的 prompt-based ReAct 已被验证有效"这个说法，在其代码里找不到依据** ——
那条路径在默认配置下从未被跑到过。这也解释了为什么我们的实验一测出 80% 遵循度：
**此前没有人真的测过它。**

### 5.3 它能跑，但那是因为有人一直在维护环境

需要明确的是：心镜**确实跑起来了**，它是一套完成度很高的平台。上面两点不是
"它做错了"，而是提醒：**框架带来的隐式依赖，其代价会以"环境问题"的形式延后出现。**

---

## 六、那 LangGraph 的"状态图"能力我们不需要吗？

需要的一部分，但我们用更小的东西覆盖了：

| LangGraph 概念 | 我们的对应 |
|---|---|
| 节点 / 边 | `RootAgent.plan()` 的五级漏斗（顺序是**被测对象**，见 `tests_funnel.py`） |
| 状态（State） | `StateStore`（单一真相源：版本 + 审计日志 + pending 队列） |
| 条件边 | `ManagerAgent._verify` 的分支（校验失败 → 丢弃并记 trace） |
| 中断 / 人工介入（interrupt） | `store.add_pending` + `POST /api/confirm`（高危二次确认） |
| Checkpointer / 持久化 | JSON / JSONL 落盘（审计日志、经验库、日报） |
| 流式事件 | WebSocket `decision_trace`（`ground` / `recall` / `propose` / `verify` / `execute` / `degrade`） |

**差异在于**：LangGraph 的状态图是**通用**的，因此也是**不可测**的 ——
你不能对"框架应该怎么流转"写断言。而灵犀的漏斗顺序、护栏链、租约状态机
**每一条都有对应的测试**（12 个脚本，共 500+ 项断言）。

> 这也是为什么本项目把"漏斗顺序"本身当作被测对象：它出错的后果是静默的
> （实测过：「窗帘开一半」被宽泛的设备正则抢成全开）。这类问题在通用状态图里
> 更难发现，因为你得先相信框架的调度是对的。

---

## 七、如果将来真要用，正确的方式是什么

不是"永不引入"，而是**先满足三个条件**：

1. **它必须落在已有的 `Gate` 抽象之上**，而不是绕过它。任何新增的调用通路
   都要经过同一条护栏链 —— 这是 D22 用血换来的教训。
2. **必须有不装它的可演示降级路径**，且该路径要被测试覆盖（不能像 §5.1 那样
   `raise ImportError` 了事）。
3. **必须带来可测量的收益**。本项目已经证明：延迟由输出 token 数决定，
   与调用次数、prompt 体积、管线结构无关（实验三、五）。所以"用框架让管线更优雅"
   **不是一个收益**。

**当前不满足第 3 条，因此不引入。**

---

## 八、一句话回答答辩提问

> **"这和直接用 LangChain Agent 有什么区别？"**
>
> 「三个区别：
> ① **护栏在架构里不在 prompt 里** —— Root 的工具全只读，每个副作用动作强制走租约链，
> 模型选错设备的结果是被拒绝而不是被执行；框架会自己长出新的调用通路，而护栏的价值
> 完全取决于它是不是唯一路径（我们就踩过这个坑，见 D22）。
> ② **证据链与可测性** —— 回读三态证据、租约状态机、五级漏斗顺序，每一条都有对应测试；
> 通用状态图很难对"框架应该怎么流转"写断言。
> ③ **零重依赖** —— 6 行 requirements 是"pip install 即跑、浏览器打开就能演示"
> 这个卖点的一部分。
>
> 而且我们**实测否决**了自己写的 ReAct 循环：格式遵循度只有 80%，
> 延迟还无优势 —— 所以这个问题在我们这里不是'用不用框架'，而是'我们不需要 ReAct'。」
