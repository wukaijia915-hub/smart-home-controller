# 全屋智能数字人中枢 · Web 3D 最小可运行原型

面向全屋异构智能家居的自主式多模态数字人中枢交互系统 —— **Web 3D 主干版最小闭环 Demo**。
纯软件、零硬件、浏览器即开即用，对应《技术实现方案 V2.0》第一阶段到第二阶段的可演示成果。

## 一、功能一览

| 模块 | 已实现能力 |
|---|---|
| 数字孪生沙盒 | Three.js 3D 客厅场景：吊灯/台灯（亮度联动）、窗帘（开合动画）、空调（指示灯）、电视（流光屏）、风扇（旋转）、加湿器（水雾粒子）、沙发/茶几/电视柜 |
| 环境仿真 | 昼夜光照循环、温湿度波动、人体红外、空气质量；与设备状态联动（开灯/开窗帘→光照上升、开空调→温度趋稳） |
| 多模态 Agent | 双层决策：①规则层（场景+设备指令，<100ms）②远端 LLM 推理层（Function Calling 风格结构化输出，配置 API Key 后启用） |
| 设备抽象层 | Device/Attribute/Command schema + 10 个虚拟设备驱动（参考 Matter cluster / HA entity 模型） |
| 能力注册表 | 能力契约与设备类别解耦 + 口语别名归一（「好热」→ climate.set_temperature）+ 孪生配对（可执行数字孪生） |
| 事件路由引擎 | 声明式事件路由（跌倒看护/离家节能/隐患预警）+ 冷却去重 + fan-out 并发 + 回读确认证据链 |
| 数字人 | 低成本 SVG 形象"小智"+ 说话口型动画 + 状态提示 |
| 交互 | 自然语言对话（支持一句话多条指令）、6 大场景模式按钮、实时设备状态面板、传感器面板 |

## 二、目录结构

```
web3d_demo/
├── backend/
│   ├── main.py               # FastAPI 入口：REST + WebSocket + 前端静态托管
│   ├── agent.py              # 决策 Agent（场景规则 + 设备指令规则 + LLM 层）
│   ├── llm_client.py         # 远端大模型客户端（OpenAI 兼容，未配置自动回退规则引擎）
│   ├── scenes.py             # 标准场景模式（回家/离家/观影/睡眠/会客/节能等）
│   ├── sensors.py            # 环境/传感器仿真
│   └── devices/              # 设备抽象层
│       ├── schema.py         # Device/Attribute/Command 模型
│       ├── virtual_devices.py# 虚拟设备驱动
│       └── registry.py       # 设备注册表 + 命令执行 + 变更事件
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   └── js/
│       ├── three.min.js      # Three.js（本地化，离线可用）
│       ├── scene.js          # 3D 客厅沙盒
│       ├── digital_human.js  # 低成本数字人
│       └── app.js            # WebSocket 通信 + UI 联动
├── requirements.txt
├── .env.example              # LLM API 配置模板
└── start.bat                 # 一键启动脚本
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
# 冒烟测试：设备层 + 规则 Agent 决策
python tests_smoke.py
# 端云协同 + 多模态测试（19 项断言）
python tests_edge_cloud.py
# 能力注册表 + 事件路由 + 执行契约测试（30 项断言）
python tests_capabilities.py
# 可执行数字孪生 + 证据链 5 场景演示
python demo_capability_route.py
```

已通过验证：
- 10 个虚拟设备增删与控制、命令范围校验
- 场景/指令规则命中（含一句话多指令）
- WebSocket 实时链路（init → chat → device_state / agent_reply）
- 传感器仿真动态更新与设备联动（开窗帘→光照上升）
- 口语别名归一（好热/太亮/拉上窗帘 → 能力契约）与设备类别契约筛选
- 执行回读确认三态（confirmed/delivered/failed）+ 孪生配对一致性证据
- 事件路由 fan-out + 冷却去重（跌倒看护/离家节能）
- 浏览器端 3D 渲染、对话交互、场景按钮（Chrome 实测）

## 八、后续路线（对应方案 V2.0 第三阶段起）

- 数字人升级：Live2D / ReadyPlayerMe + uLipSync（3D 化）
- 语音链路：浏览器 Web Speech API / FunASR + Edge-TTS / CosyVoice
- 场景扩展：卧室 3D 化、更多设备类型
- 真实设备：Matter / Home Assistant 协议对接（虚拟设备驱动替换）
- 开放接入 SDK 完善 + 设备注册/发现
