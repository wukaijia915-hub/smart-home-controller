# 全屋智能数字人中枢 · Web 3D 最小可运行原型

面向全屋异构智能家居的自主式多模态数字人中枢交互系统 —— **Web 3D 主干版最小闭环 Demo**。
纯软件、零硬件、浏览器即开即用，对应《技术实现方案 V2.0》第一阶段到第二阶段的可演示成果。

## 一、功能一览

| 模块 | 已实现能力 |
|---|---|
| 数字孪生沙盒 | Three.js 3D 客厅场景：吊灯/台灯（亮度联动）、窗帘（开合动画）、空调（指示灯）、电视（流光屏）、风扇（旋转）、加湿器（水雾粒子）、沙发/茶几/电视柜 |
| 环境仿真 | 昼夜光照循环、温湿度波动、人体红外、空气质量；与设备状态联动（开灯/开窗帘→光照上升、开空调→温度趋稳） |
| 多模态 Agent | 双层决策：①规则层（场景+设备指令，<100ms）②远端 LLM 推理层（结构化 JSON 输出，配置 API Key 后启用；无 Key 时全链路仍可演示） |
| 设备抽象层 | Device/Attribute/Command schema + **17 台虚拟设备 / 12 个类别**（参考 Matter cluster / HA entity 模型） |
| 工具层 | **64 个标准化工具**，schema 由设备属性元数据反推（永不漂移），支持 MCP 动态发现与调用 |
| 可信执行链 | 授权租约 + 状态版本校验 + 参数校验 + 高危二次确认 + 回读三态证据（confirmed/delivered/failed） |
| 能力注册表 | 能力契约与设备类别解耦 + 39 条口语别名归一（「好热」→ climate.set_temperature）+ 孪生配对（可执行数字孪生） |
| 事件路由引擎 | 声明式事件路由（跌倒看护/离家节能/隐患预警）+ 冷却去重 + fan-out 并发 + 回读确认证据链 |
| 数字人 | 低成本 SVG 形象"小智"+ 说话口型动画 + 状态提示 |
| 交互 | 自然语言对话（支持一句话多条指令）、6 大场景模式按钮、实时设备状态面板、传感器面板 |

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
│   │   └── registry.py            工具注册表 + 参数校验 + 调用日志（可审计）
│   ├── tools.py                 兼容垫片 → 转发到 toolkit/
│   ├── lease.py                 授权租约 + 高危操作策略（lock/gas_stove/camera）
│   ├── state.py                 单一真相源：版本递增 + 审计日志 + pending 队列
│   ├── capabilities.py          能力契约 + 口语别名归一 + 孪生配对
│   ├── event_routes.py          声明式事件路由（冷却去重 + fan-out 并发）
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
│   │   └── event_routes.json      3 条事件路由
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
├── experiments/                 ★ 方案选型的实测依据（ReAct 遵循度 / 幻觉率 / 延迟归因）
├── tests_*.py                   7 个回归测试脚本（见第七节）
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
| WS | `/ws` | 实时双向（指令上行 + 状态/传感器/回复下行） |

## 七、验证与测试

```bash
# 全量门禁（每个阶段收尾必须七脚本全绿）
LLM_OFFLINE=1 python tests_smoke.py         # 设备层 + 规则决策冒烟
LLM_OFFLINE=1 python tests_agents.py        # 租约 / 版本校验 / 幻觉拦截 / 高危二次确认
LLM_OFFLINE=1 python tests_capabilities.py  # 能力注册表 + 事件路由 + 执行契约（30 项）
LLM_OFFLINE=1 python tests_edge_cloud.py    # 隐私分级 / 脱敏 / 端云路由（19 项）
LLM_OFFLINE=1 python tests_error.py         # 7 类异常与边界输入（26 项）
LLM_OFFLINE=1 python tests_funnel.py        # 五级漏斗优先级 + 架构不变量 + 护栏不被绕过（38 项）
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
| 规则层响应延迟 | < 100ms | 平均 7.4ms / p95 18ms | ✅ |
| 设备控制成功率 | ≥ 95% | 64/64 = 100% | ✅ |
| 决策准确率（标准场景集 S1–S11） | ≥ 90% | 离线 100%（90/90） | ✅ |
| 前端渲染帧率 | ≥ 30 FPS | 待浏览器实测 | — |
| 慢环 LLM 端到端延迟 | < 5s | **11.6–14.0s** | ❌ 见下 |

> **关于慢环延迟（2026-10-02 实测）**：单次调用 5.2–8.7s，而最小请求基线仅 642ms ——
> 瓶颈在**模型生成速度**，不在网络、调用次数或 prompt 体积。Manager 的逐子任务翻译已
> 改为批量翻译（LLM 调用数 `1+N` → `1+1`，trace 已验证生效），但墙上时间被单次延迟主导。
> 故本项目**不声称慢环 P95 <5s**，而是走「预算化降级、P95 有界」；换更快模型或本地推理
> 即可线性改善。规则层 7.4ms 是确定性路径，无外部依赖。

## 八、后续路线

详见 `docs/融合技术白皮书.md`。近期方向：

- **感知层**：多模态置信度融合（视觉行为 + 面部表情 + 环境行为 + 前庭振动），
  模态一致性复核；全部端侧推理，仅标签出网
- **记忆层**：习惯统计 + 设备手册 + 成功案例经验复用三源
- **快慢双环**：秒级控制环（已有）+ 22:00 家庭日报生成环（LLM 优先 / 模板降级）
- **决策层**：五级意图漏斗 + prompt-based ReAct 慢环（适配不支持 function calling 的国产模型）
- **数字人升级**：Live2D / ReadyPlayerMe + uLipSync（3D 化）
- **真实设备**：Matter / Home Assistant 协议对接（替换虚拟设备驱动，能力契约不变）
- **国产算力**：感知层计算密集路径接入 MUSA→CUDA→numpy 自动探测链
