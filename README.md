# 灵犀 · 门控式大模型家居中枢 — Web 3D 可运行原型

面向全屋异构智能家居的可审计大模型执行与门控式多模态感知中枢 —— **Web 3D 版可运行原型**。
纯软件、零硬件、浏览器即开即用，对应《技术实现方案 V2.0》第一阶段到第二阶段的可演示成果。

## 一、功能一览

| 模块 | 已实现能力 |
|---|---|
| 数字孪生沙盒 | Three.js 3D 客厅场景：吊灯/台灯（亮度联动）、窗帘（开合动画）、空调（指示灯）、电视（流光屏）、风扇（旋转）、加湿器（水雾粒子）、沙发/茶几/电视柜 |
| 环境仿真 | 昼夜光照循环、温湿度波动、人体红外、空气质量；与设备状态联动（开灯/开窗帘→光照上升、开空调→温度趋稳） |
| 多模态 Agent | 双层决策：①规则层（场景+设备指令，<100ms）②远端 LLM 推理层（结构化 JSON 输出，配置 API Key 后启用；无 Key 时全链路仍可演示） |
| 设备抽象层 | Device/Attribute/Command schema + **17 台虚拟设备 / 12 个类别**（参考 Matter cluster / HA entity 模型） |
| 工具层 | **64 个标准化工具**，schema 由设备属性元数据反推（永不漂移），支持 MCP 动态发现与调用 |
| 多模态感知 | 4 路模态（视觉行为 / 环境行为 / 面部表情 / 前庭）→ 置信度自适应加权 → **一致性复核，冲突即丢弃**；原始画面不出户 |
| 白盒情境参数 | 5 个参数（在家时长 / 活动强度 / 作息偏离 / 舒适度偏差 / 异常标志），每个带真值来源与原始依据，可展开复核 |
| 可信执行链 | 授权租约 + 状态版本校验 + 参数校验 + 高危二次确认 + 回读三态证据（confirmed/delivered/failed） |
| 能力注册表 | 能力契约与设备类别解耦 + 39 条口语别名归一（「好热」→ climate.set_temperature）+ 孪生配对（可执行数字孪生） |
| 事件路由引擎 | 声明式事件路由（跌倒看护/离家节能/环境联动/隐患预警）+ 冷却去重 + **高危动作默认拒绝** + 回读确认证据链 |
| **记忆三源** | 设备手册检索 + 习惯统计（含「显式声明 vs 实际行为」偏好一致性校验）+ 经验复用（成功案例回灌慢环 prompt，**确定性排序**，可复核注入了哪几条） |
| **快慢双环** | 内环：2s 状态 / 5s 声明式阈值告警 / 15s 感知评估（**零 LLM**）。外环：22:00 家庭日报（**LLM 优先，模板降级**，断网也出文案） |
| **可观测性** | IP 滑窗限流（普通 300/min、LLM 10/min，加锁）+ API 统计 + 模型调用日志 + **决策分层延迟**（规则层 p95 与慢环 p95 分开报） |
| 数字人 | 低成本 SVG 形象"小智"+ 说话口型动画 + 状态提示 |
| 交互 | 自然语言对话（支持一句话多条指令）、6 大场景模式按钮、实时设备状态面板、传感器面板、家庭情境看板、记忆与观测面板 |

## 二、目录结构

```
smart-home-controller/
├── backend/
│   ├── main.py                  FastAPI 入口：装配单例、挂路由、lifespan 后台循环
│   ├── agent.py                 HomeAgent 门面：端云路由 → Root → Manager 决策链
│   ├── agents/                  ★ 多智能体决策层
│   │   ├── root_agent.py          Root：编排者，只提议 + 授权，不执行
│   │   └── manager_agent.py       Manager：翻译 + 校验 + 执行（最后一道闸门）
│   ├── toolkit/                 ★ 工具层：LLM 影响物理世界的唯一出口
│   │   ├── schema_gen.py          从设备属性元数据反推工具契约（schema 永不漂移）
│   │   ├── registry.py            工具注册表 + 参数校验 + 调用日志（可审计）
│   │   └── guard.py               ManagerGuard：工具路径复用 Manager 同一条护栏链
│   ├── tools.py                 兼容垫片 → 转发到 toolkit/
│   ├── gate.py                  ★ Gate 抽象（感知层与执行层共用同一套门控接口）
│   ├── perception/               ★ 感知层
│   │   ├── fusion.py               多模态置信度融合（移植心镜，二模态→N 模态）
│   │   ├── modal_gate.py           MultiModalGate：一致性复核，冲突即丢弃
│   │   ├── situation.py            5 个白盒情境参数（每个带真值来源与原始依据）
│   │   ├── service.py              感知入口：模态 → 融合 → 门控 → 询问式建议
│   │   ├── gpu.py                  算力后端探测（当前无热路径，见文件头说明）
│   │   └── modalities/             视觉行为 / 环境行为 / 面部表情(演示级) / 前庭(适配器)
│   ├── memory/                   ★ 记忆层三源（只读世界，不写世界）
│   │   ├── manuals.py              设备手册检索（含共用相关度打分函数）
│   │   ├── habits.py               习惯统计 + 显式偏好一致性校验
│   │   └── experience.py           经验复用（成功案例回灌 prompt，确定性排序）
│   ├── observability/            ★ 可观测性三件套（全部加锁）
│   │   ├── rate_limit.py           IP 滑窗限流
│   │   ├── api_log.py              API 统计
│   │   └── llm_log.py              模型调用日志 + 决策分层延迟
│   ├── jobs/daily_summary.py     ★ 外环：22:00 家庭日报（LLM 优先 / 模板降级）
│   ├── alerts.py                 ★ 内环告警：读数 → 声明式阈值 → 结构化事件
│   ├── lease.py                 授权租约 + 高危操作策略（lock/gas_stove/camera）
│   ├── state.py                 单一真相源：版本递增 + 审计日志 + pending 队列
│   ├── capabilities.py          能力契约 + 口语别名归一 + 孪生配对
│   ├── event_routes.py          声明式事件路由（冷却去重 + 同设备串行 + 高危默认拒绝）
│   ├── edge_cloud.py            端云隐私分级（L0/L1/L2）+ 脱敏 + 上云审计
│   ├── multimodal.py            端侧视觉事件检测（画面本地处理，不上云）
│   ├── mcp.py                   MCP Server（JSON-RPC 2.0）
│   ├── channels.py              多渠道接入（HTTP / Webhook / MQTT 模拟）
│   ├── skills.py                用户自定义技能（动作组合）
│   ├── scheduler.py             APScheduler 定时任务
│   ├── scenes.py                10 个标准场景
│   ├── sensors.py               环境/传感器仿真（含昼夜光照循环）
│   ├── config_loader.py         JSON 配置热重载
│   ├── llm_client.py            多后端 LLM 客户端（智谱/DeepSeek/通义/Moonshot）
│   ├── config/                  JSON 配置驱动（不硬编码进 Python）
│   │   ├── devices.json           17 台设备 / 12 个类别
│   │   ├── scenes.json            10 个场景
│   │   ├── environment.json       传感器 / 昼夜循环 / 联动系数 / 告警阈值
│   │   ├── capabilities.json      16 条能力契约 + 39 条口语别名
│   │   ├── event_routes.json      10 条事件路由（跌倒看护/离家节能/隐患/环境联动/设备健康）
│   │   ├── alert_routes.json      5 条内环告警阈值（声明式，改阈值不改代码）
│   │   ├── situation_mapping.json 情境参数权重 + 区域判定规则（声明式）
│   │   └── loops.json             快慢双环周期与外环日报时刻（演示时可现改）
│   ├── devices/                 设备抽象层
│   │   ├── schema.py              Device / Attribute / Command 模型
│   │   ├── virtual_devices.py     虚拟设备驱动 + apply_command 命令语义
│   │   └── registry.py            设备注册表 + 回读确认 + 孪生同步
│   └── knowledge/               知识库（设备手册 + 用户习惯统计）
├── frontend/                    原生 HTML/CSS/ES Module + Three.js r160（无构建工具）
│   ├── index.html
│   ├── assets/three.module.js
│   ├── models/*.glb             10 个 GLB 模型（房屋/家具/绿植）
│   └── js/{scene,digital_human,app}.js
├── experiments/                 ★ 方案选型的实测依据（含被否决的路线 + bench_collect.py）
├── docs/                        ★ 6 份设计文档（见第九节）
├── data/                        运行时状态（习惯/经验/日报/日志），已 gitignore
├── demo_fusion.py               融合能力演示（10 个演示点，全程离线）
├── tests_*.py                   12 个回归测试脚本（见第七节）
├── demo_*.py                    场景演示脚本
├── requirements.txt
├── .env.example                 LLM API 配置模板
├── CLAUDE.md                    ★ 开发约定（改动前必读）
├── LICENSE / NOTICE             许可证与第三方来源声明
└── start.bat                    一键启动脚本
```

## 三、快速启动

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 启动服务
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 或 Windows 下双击 start.bat
```

浏览器打开 **http://127.0.0.1:8000** 即可使用。

> 提示：安装依赖若遇 pip 配置报错（`pip.conf invalid utf-8`），检查
> `C:\Users\<用户>\AppData\Local\Doubao\User Data\sandbox_runtime\bases\<id>\pip.conf`
> 是否含 GBK 编码的中文路径，改为 UTF-8 保存即可。

## 四、演示话术（建议现场使用）

| 指令 | 预期效果 |
|---|---|
| 「我回家了」 | 开客厅灯（80%）、拉开窗帘、开空调 26℃ |
| 「看电影」 | 灯光调暗 20%、色温暖 3000K、关窗帘、开电视 |
| 「睡觉了」 | 关主灯、夜灯微光、空调自动模式、关窗帘 |
| 「打开窗帘，空调调到24度」 | 窗帘全开 + 空调 24℃（一句话多指令） |
| 「好热」 | 空调 24℃ + 风扇开启 |
| 「卧室开灯」 | 打开卧室灯 |
| 「电视音量30」 | 电视音量调到 30 |

## 五、接入远端大模型（可选增强）

默认未配置 API Key 时使用内置**规则引擎**（场景 + 设备指令识别），端到端可演示。

如需启用 LLM 推理层（模糊指令、复杂联动）：

```bash
# 复制 .env.example 为 .env 并填写
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=你的Key
LLM_MODEL=deepseek-chat
```

支持任何 OpenAI 兼容接口（DeepSeek / 通义千问 DashScope 等）。配置后 `/api/config` 返回 `llm_configured: true`，Agent 在规则层未命中时自动调用 LLM 规划。

## 六、接口规范

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 前端启动配置（设备/传感器/场景） |
| GET | `/api/devices` | 设备清单与状态 |
| GET | `/api/sensors` | 传感器实时数据 |
| POST | `/api/chat` | 文本对话 `{"text":"我回家了"}` |
| POST | `/api/scene` | 场景切换 `{"scene":"movie"}` |
| POST | `/api/control` | 开放接入 SDK：直接控制设备 |
| POST | `/api/edge` | 边缘场景：模拟设备离线/故障 |
| GET | `/api/capabilities` | 能力清单 + 口语别名表 + 孪生对状态（可执行数字孪生） |
| POST | `/api/events/route` | 注入结构化事件，路由为设备动作 + 告警（含回读证据） |
| GET | `/api/perception` | 最近一次感知结果（模态读数 + 门控 + 5 个情境参数 + 询问式建议） |
| POST | `/api/perception/observe` | 按给定上下文跑一次感知（演示用，只出建议不出动作）。支持 `now_hour` 指定判定时刻，演示深夜场景不必等到深夜 |
| GET | `/api/perception/status` | 感知层状态：模态可用性 / 门控阈值 / 算力后端 |
| GET | `/api/memory/stats` | 记忆三源统计（手册 / 习惯 / 经验），含偏好冲突与评分来源区分 |
| GET | `/api/memory/experience` | 检索经验库（与慢环 prompt 注入用**同一个确定性排序**，所见即模型所见） |
| POST | `/api/memory/feedback` | 用户显式评分 `{id, rating 1-5, feedback}` —— 经验库中唯一衡量满意度的信号 |
| GET | `/api/metrics/llm` | **分层延迟**（规则层/慢环 avg+p95）、降级率、token、最近调用 |
| GET | `/api/metrics/api` | 接口统计 + 限流状态 + 感知层/告警层状态 |
| GET | `/api/alerts/config` | 当前生效的告警阈值（阈值不在代码里，在这个 JSON 里，可复核） |
| POST | `/api/alerts/evaluate` | 按给定读数**预演**告警判定（dry-run，不产生任何设备动作，不污染冷却状态） |
| GET | `/api/jobs/status` | 调度器状态：内部作业（日报）下次触发时刻 |
| GET | `/api/jobs/daily_summary/latest` | 最近一份家庭日报 |
| POST | `/api/jobs/daily_summary/run` | 手动生成日报（演示用，不必等到 22:00），走与定时任务**同一条**路径 |
| WS | `/ws` | 实时双向（指令上行 + 状态/传感器/回复/日报下行） |

## 七、验证与测试

```bash
# 全量门禁（每个阶段收尾必须 12 脚本全绿）
export LINGXI_DATA_DIR=$(mktemp -d)          # 隔离运行时数据，勿写真实用户数据
LLM_OFFLINE=1 python tests_smoke.py         # 设备层 + 规则决策冒烟
LLM_OFFLINE=1 python tests_agents.py        # 租约 / 版本校验 / 幻觉拦截 / 高危二次确认
LLM_OFFLINE=1 python tests_capabilities.py  # 能力注册表 + 事件路由 + 执行契约（30 项）
LLM_OFFLINE=1 python tests_edge_cloud.py    # 隐私分级 / 脱敏 / 端云路由（19 项）
LLM_OFFLINE=1 python tests_error.py         # 7 类异常与边界输入（26 项）
LLM_OFFLINE=1 python tests_funnel.py        # 五级漏斗优先级 + 架构不变量 + 护栏不被绕过（48 项）
LLM_OFFLINE=1 python tests_budget.py        # 慢环预算：次数/墙钟/超时上限 + 降级可见（25 项）
LLM_OFFLINE=1 python tests_perception.py    # 融合/门控/情境参数 + 感知层硬边界静态检查（106 项）
LLM_OFFLINE=1 python tests_memory.py        # 记忆三源 + 经验复用闭环 + 记忆层无执行能力（66 项）
LLM_OFFLINE=1 python tests_observability.py # 限流并发安全 / 分层延迟 / 降级率（48 项）
LLM_OFFLINE=1 python tests_loops.py         # 内环告警 / 外环日报降级 / 事件路由高危拒绝（68 项）
LLM_OFFLINE=1 python tests_performance.py   # 量化验收指标

# 演示脚本
python demo_capability_route.py     # 可执行数字孪生 + 证据链 5 场景
python demo_edge_cloud.py           # 端云协同 5 场景
```

**门禁必须带 `LLM_OFFLINE=1`**：否则测试会打真实网络请求，结果随模型输出波动而 flaky
（已实测复现：模型返回 JSON 数组时 `manager._llm_translate` 曾崩溃）。
`LLM_OFFLINE=1` 强制不发起任何网络请求，让回归只测我们自己的代码。

**离线模式**：加 `LLM_OFFLINE=1` 可强制不发起任何网络请求，全部走规则与结构化路径。
用于「断网演练」演示，也用于让回归测试可复现（不依赖网络与模型输出波动）：

```bash
LLM_OFFLINE=1 python tests_performance.py
```

### 当前实测指标

| 指标 | 目标 | 实测 | 结论 |
|---|---|---|---|
| 规则层响应延迟 | < 100ms | 平均 **0.13ms** / p95 0.26ms | ✅ |
| 设备控制成功率 | ≥ 95% | 64/64 = 100% | ✅ |
| 决策准确率（标准场景集 S1–S11） | ≥ 90% | 离线 100%（90/90） | ✅ |
| 前端渲染帧率 | ≥ 30 FPS | 待浏览器实测 | — |
| 慢环 LLM 端到端延迟 | < 5s | **11.6–14.0s** | ❌ 见下 |
| 慢环降级率 | 无红线，但须可见 | 见 `/api/metrics/llm` | — |

> **分层延迟不要合成一个数**：规则层是纯内存查表（毫秒），慢环是模型生成（秒），
> 合成「平均响应时间」既不能解释性能也不能解释体验。`/api/metrics/llm` 返回
> 真实决策样本的 `rule_path` / `slow_path` 各自的 avg 与 p95，**评委可当场调这个接口对**，
> 不要在文档里手填数字。

> **关于慢环延迟（2026-10-02 实测）**：单次调用 5.2–8.7s，而最小请求基线仅 642ms ——
> 瓶颈在**模型生成速度**，不在网络、调用次数或 prompt 体积。Manager 的逐子任务翻译已
> 改为批量翻译（LLM 调用数 `1+N` → `1+1`，trace 已验证生效），但墙上时间被单次延迟主导。
> 故本项目**不声称慢环 P95 <5s**，而是走「预算化降级、P95 有界」；换更快模型或本地推理
> 即可线性改善。规则层 7.4ms 是确定性路径，无外部依赖。

## 八、演示建议（阶段 4 新增的部分）

| 演示动作 | 看点 |
|---|---|
| 点开右上「记忆与观测」面板 | **分层延迟**：规则层 avg/p95 与慢环 avg/p95 分开显示，降级率可见 |
| 对着空调说几句「有点闷」类模糊指令（须走到慢环） | 决策链路里出现紫色的 **RECALL** 步：写明本轮参考了哪条历史案例、评分多少 |
| `curl /api/memory/stats` | 经验库条数与**评分来源区分**（`auto_execution` 是执行结果，`user` 才是满意度） |
| 点「生成」按钮 | 日报当场出（不必等 22:00）。**拔掉 API Key / `LLM_OFFLINE=1` 再点一次** → 标签从「LLM 生成」变「模板降级」，文案仍有信息量 |
| 把 `config/alert_routes.json` 温度阈值改成 25 | 5 秒内数字人告警 + 空调自动调到 24℃（阈值在配置里，不改代码不重启） |
| `POST /api/alerts/evaluate` 传 `{"sensors":{"temp_indoor":33}}` | 预演判定，**不产生任何设备动作**，也不污染正在跑的告警冷却 |
| `POST /api/perception/observe` 传 `now_hour: 2` | 深夜场景当场复现（否则要等到凌晨 2 点才能演示「深夜活动」判定） |

## 九、设计文档

| 文档 | 内容 | 什么时候读它 |
|---|---|---|
| [docs/融合技术白皮书.md](docs/融合技术白皮书.md) | 融合叙事、统一设计哲学、创新点对照表、逐项诚实边界 | 想了解"这次升级做了什么、为什么" |
| [docs/架构图.md](docs/架构图.md) | 8 张架构图（L0–L8 / 统一门控 / 五级漏斗 / 租约状态机 / 双环 / 融合流程 / 经验闭环 / 告警 vs 感知） | **架构图的单一出处**，别处不要复制 |
| [docs/BENCH.md](docs/BENCH.md) | 性能数据、采集方法、原始 JSON、**以及"这些数字不能用来声称什么"** | 要引用任何性能数字之前 |
| [docs/感知层可靠性报告.md](docs/感知层可靠性报告.md) | 逐模态验证边界、已知失效模式清单、阈值来源 | 要谈感知层能力之前 |
| [docs/为什么不用LangChain.md](docs/为什么不用LangChain.md) | 技术选型论证（含对上游 LangGraph 用法的源码核对） | 被问"为什么不用框架"时 |
| [docs/答辩Q&A预案.md](docs/答辩Q&A预案.md) | 20 个问答、演示顺序、**危险表述清单**、提交前待办 | 答辩前 |
| [experiments/README.md](experiments/README.md) | 5 项实验记录（含**被否决的路线**及其数据） | **改动架构决策前必读**，避免重复已被证伪的路 |

## 十、后续路线

近期方向：

- **接入真实视觉模型**（YOLO / 姿态估计）并补混淆矩阵 —— 优先级最高，
  让感知层"最可靠的一路"从模拟变成实测
- **融合层阈值标定**（`MODAL_DISTANCE_THRESHOLD` 等三个常量沿用上游，未做敏感性分析）
- **护栏加固**：租约参数包络 + `verify` 全路径（阶段 2，有明确陷阱与回退预案）
- **决策层**：prompt-based ReAct 慢环 —— **已实测否决**（格式遵循度 80% 且延迟无优势），
  写入 Future Work 并附数据，见 `experiments/README.md`
- **数字人升级**：Live2D / ReadyPlayerMe + uLipSync（3D 化）
- **真实设备**：Matter / Home Assistant 协议对接（替换虚拟设备驱动，能力契约不变）
- **国产算力**：**待接入真实视觉负载之后**再进行
  （当前无热路径，不得作为成果主张 —— `gpu.status()` 会明确警告）
