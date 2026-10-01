# CLAUDE.md — 灵犀（LingXi）智能家居数字人中枢

本文件是本仓库的开发约定，**所有改动必须先读本文件**。需要调整约定时，先改本文件、再改代码。

---

## 一、项目定位

**灵犀 2.0**：面向全屋异构智能家居的「确定性内核 + 可编排 LLM 外壳 + 门控式多模态感知」端云协同数字人中枢。

核心设计哲学（**贯穿全栈，改动时不可违背**）：

> **同一个"不信任单一信号源"的原则，贯穿感知层与执行层。**

| 层 | 单一信号源的风险 | 门控机制 |
|---|---|---|
| 感知层 | 单模态误判（光照导致面部误判等） | 多模态置信度自适应加权 + 模态一致性复核，冲突即丢弃 |
| 执行层 | LLM 幻觉 / 越权 / 重放 | 授权租约 + 参数包络 + 状态版本校验 + 高危二次确认 |

两侧共用 `backend/gate.py` 的 `Gate` 抽象。**新增任何门控逻辑时，必须实现 `Gate` 接口，不要另起一套。**

---

## 二、目录结构约定

```
smart-home-controller/
├── backend/
│   ├── main.py              FastAPI 入口：装配单例、挂路由、中间件、lifespan 后台循环
│   ├── gate.py              ★ Gate 抽象（感知层与执行层共用）
│   ├── agents/              多智能体决策层
│   │   ├── root_agent.py      Root：编排者，只提议+授权，不执行
│   │   ├── manager_agent.py   Manager：翻译+校验+执行（最后一道闸门）
│   │   ├── react_loop.py      ★ prompt-based ReAct 循环（慢环用）
│   │   └── llm_budget.py      ★ LLM 调用预算（次数/token/墙钟）
│   ├── toolkit/             ★ 工具层（LLM 影响世界的唯一出口）
│   │   ├── schema_gen.py      从 Device.attributes 反推 schema，永不漂移
│   │   ├── registry.py        ToolRegistry
│   │   ├── views.py           root_tools() 只读 / manager_tools() 可写
│   │   ├── meta_tools.py      元工具实现
│   │   └── guard.py           ActuationGuard：工具层↔护栏层的唯一接缝
│   ├── perception/          ★ 感知层
│   │   ├── fusion.py          多模态置信度融合
│   │   ├── situation.py       白盒情境参数
│   │   ├── gpu.py             国产算力探测链（MUSA→CUDA→numpy）
│   │   └── modalities/        各模态检测器
│   ├── memory/              ★ 记忆层三源
│   │   ├── habits.py          用户习惯统计
│   │   ├── manuals.py         设备手册检索
│   │   └── experience.py      经验复用（成功案例回灌 prompt）
│   ├── observability/       ★ 可观测性
│   │   ├── rate_limit.py      IP 滑窗限流
│   │   ├── api_log.py         API 统计（加锁）
│   │   └── llm_log.py         LLM 调用日志（JSONL）
│   ├── jobs/daily_summary.py ★ 外环：22:00 家庭日报
│   ├── lease.py             授权租约 + 参数包络
│   ├── state.py             单一真相源：版本 + 审计日志 + pending 队列
│   ├── capabilities.py      能力契约 + 别名归一 + 孪生配对
│   ├── event_routes.py      声明式事件路由
│   ├── edge_cloud.py        端云隐私分级 L0/L1/L2
│   ├── devices/             设备抽象 + 虚拟设备工厂
│   ├── config/              JSON 配置（devices/scenes/environment/capabilities/event_routes/alert_routes）
│   └── knowledge/           兼容垫片 → 转发到 memory/
├── frontend/                原生 JS + Three.js，无构建工具
├── docs/                    ★ 设计文档（架构图/BENCH/白皮书/选型论证）
├── tests_*.py               根目录的回归测试脚本
└── requirements.txt
```

**放哪里的规则**
- 新能力 → 先问「这是感知、决策、工具、护栏、记忆、观测中的哪一层」，放对应目录，不要塞进 `main.py`
- 新配置 → `backend/config/*.json`，**不要硬编码进 Python**
- 新文档 → `docs/`，命名用中文短语
- 临时脚本/调试产物 → **不要提交**，用完删

---

## 三、硬指标（改动后必须复测，不可退化）

| 指标 | 当前实测 | 红线 |
|---|---|---|
| 规则层响应延迟 | 平均 7.4ms，p95 18ms | **平均 <10ms，p95 <100ms** |
| 设备控制成功率 | 64/64 = 100% | **100%** |
| 决策准确率（标准场景集 S1-S11） | 离线 100%（90/90） | **≥90%** |
| 前端渲染帧率 | 56 FPS（待浏览器复测） | ≥30 FPS |
| 慢环 LLM 端到端延迟 | **11.6–14.0s**（未达标） | P95 <5s（**当前服务商下达不到，见下**） |

**关于决策准确率**：S1-S11 是回归套件，规则层应覆盖 100%（现已达到）。LLM 的价值体现在
套件之外的模糊/多步指令，由 `tests_performance.py --api` 单独测量。不要笼统宣称
"LLM 决策准确率 100%" —— 评委问「把 API Key 删掉再跑一遍」就会穿帮。

**关于慢环延迟（2026-10-02 实测，重要）**：单次 LLM 调用耗时 5.2s（Root）与 8.7s（Manager
批量），而最小请求基线仅 642ms —— 说明**瓶颈在模型生成速度，不在网络、调用次数或 prompt
体积**。已做的优化（Manager 批量翻译，把 LLM 调用数从 `1+N` 降到 `1+1`）在 trace 上已验证
生效，但墙上时间被单次延迟主导，故总耗时仍是 11.6–14.0s。

**因此不要声称"慢环 P95 <5s"**。正确表述是：
- 规则层 P50 7.4ms（确定性，无外部依赖）
- 慢环走**预算化降级**（`llm_budget.py`），P95 有界
- 换更快模型或本地推理即可线性改善（调用次数已是最优）

另注：`.env` 里 `LLM_TIMEOUT=30`，超时保护实际形同虚设；演示前建议下调到 10s，
或由 `llm_budget` 统一约束（阶段 1b）。

**规则层红线是物理保证**：五级漏斗 ①~⑤ 全部在 `RootAgent.plan()` 内同步完成，**任何情况下都不得触碰 LLM**。新增规则时如果发现自己需要调 LLM，说明它属于慢环。

---

## 四、验证命令

**每次改完必须跑，不允许"只改不验"**：

```bash
cd smart-home-controller

# 快速回归（改小东西时）
LLM_OFFLINE=1 python tests_smoke.py

# 全量门禁（每个阶段收尾必须六脚本全绿）
LLM_OFFLINE=1 python tests_smoke.py \
  && LLM_OFFLINE=1 python tests_agents.py \
  && LLM_OFFLINE=1 python tests_capabilities.py \
  && LLM_OFFLINE=1 python tests_edge_cloud.py \
  && LLM_OFFLINE=1 python tests_error.py \
  && LLM_OFFLINE=1 python tests_performance.py
```

**门禁必须带 `LLM_OFFLINE=1`**：否则测试会打真实网络请求，结果随模型输出波动而 flaky
（已实测复现：`manager_agent._llm_translate` 曾因模型返回 JSON 数组而崩溃）。
`LLM_OFFLINE=1` 强制不发起任何网络请求，让回归测试只测我们自己的代码。
需测 LLM 路径时单独跑 `python tests_performance.py --api`，并接受其结果有波动。

**手动跑服务**：
```bash
python -m uvicorn backend.main:app --reload --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

**关键端点自检**：
```bash
curl -X POST localhost:8000/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
curl -X POST localhost:8000/api/tools/call -H "Content-Type: application/json" \
  -d '{"name":"...","params":{...}}'
```

---

## 五、开发纪律

**分层不可违反**
- `RootAgent` **只提议、只授权，绝不执行设备**
- `ManagerAgent` 是唯一执行入口，且必须走 `LeaseValidator`
- 执行层（`devices/registry.control`）不感知 LLM
- **编排层不得直接操作 ORM/数据库**（灵犀无 ORM，落盘一律走 JSON/JSONL）
- **感知层不得产生 lease**——见下方红线

**红线（代码级不可违背）**

1. **感知结果绝不进入控制回路**。`PERCEPTION_CANNOT_ACTUATE = True`。融合输出只能生成**询问式**话术（"要不要把灯调暖一点？"），绝不自动执行设备动作。
2. **LLM 绝不直连设备**。LLM 只能通过 `toolkit` 的工具影响世界，且每个副作用工具内部强制走 `ActuationGuard`。
3. **原始画面/音频绝不出家庭**。视觉链路一律端侧推理，只把结构化标签上云，复用 `edge_cloud` 的 L0 分级。
4. **不做情绪测量**。面部表情等信号标注为**演示级**，如实报告验证边界，不做准确率承诺。
5. **规则层不得引入 LLM**（见第三节）。

**其他**
- 测试/估算不出真值时，**如实说明边界**，不要用形容词代替数据（"大幅提升"→ 给数字）
- 不要为了让代码跑起来注释掉报错或加绕过标记，找根本原因
- 密钥、token、密码不进代码、不进 commit、不进日志。`.env` 已在 `.gitignore`，**永不提交**
- 新增第三方依赖前先问：能否用标准库？`requirements.txt` 的"5 行、pip install 即跑"是核心卖点，**重型依赖（如 TensorFlow）必须放 `requirements-vision.txt` 并保证未安装时优雅降级**

---

## 六、当前阶段

**阶段 1b：ReAct 慢环 + 工具元化**（进行中）

- ✅ 阶段 0　修地基（commit `6b37794`）
- ✅ 阶段 1a 五级漏斗接线（技能 + 能力契约接入主链路、条件句保护、批量翻译）
- ⏳ 阶段 1b 工具层元化 + `react_loop.py` + `llm_budget.py`
- ⏳ 阶段 2　租约包络与 `verify` 全路径
- ⏳ 阶段 3　感知层（多模态置信度融合 / 白盒情境参数 / 国产算力链）
- ⏳ 阶段 4　记忆三源与快慢双环
- ⏳ 阶段 5　文档与竞赛资产

完整方案：`C:\Users\Lenovo\.claude\plans\c-users-lenovo-desktop-aigc5-1-xinjing-federated-perlis.md`

**已知缺陷清单**（编号沿用方案文档，修一个划一个）

阶段 0 已修复：
- ~~D1~~ `main.py` 未 import `Request` → `/mcp`、`/api/webhook/{source}` NameError　**已修**
- ~~D2~~ `tools.py` 12 处 `dev.execute()` 调用不存在的方法　**已修**（重写为 `backend/toolkit/`，64 工具全部可用）
- ~~D3~~ `skills.py` 调用不存在的 `registry.execute()`　**已修**（并堵上"技能绕过高危确认"的洞）
- ~~D4~~ `requirements.txt` 缺 `apscheduler`　**已修**（并加 ImportError 保护，缺失时降级而非启动失败）
- ~~D5~~ `devices/registry.py` 29 行死代码　**已删**
- ~~D10~~ `registry.get_all_states()` 不存在　**已修**（那两处广播本就是冗余的，直接移除）
- ~~D11~~ 孪生从未配对 → `twin_pairs` 恒空　**已修**（启动自动配对 17 台，孪生证据链已可演示）
- ~~D12~~ `EventRouter(state_store=None)` → 事件路径不 bump 版本　**已修**
- ~~D13~~ 工具 schema 参数名与 `apply_command` 漂移　**已修**（schema 改为从设备属性反推）
- ~~D14~~ `tests_performance.py` 调 `HomeAgent._llm_plan`　**已修**
- ~~D15~~ `_command_target_value` 分支顺序错误，`start_clean` 被布尔分支截胡，回读永久 delivered　**已修**
- ~~D16~~ `LLMBackend.chat_json` 把模型返回的 JSON 数组原样返回，上层 `.get()` 崩溃　**已修**（契约收紧为 dict|None）

阶段 1a 已修复（五级漏斗接线）：
- ~~D8~~ `capabilities.py`（漏斗④）与 `skills.py`（漏斗③）接入对话主链路　**已修**
  （顺带发现并修复：`窗帘开一半` 被宽泛设备正则抢成全开 —— 能力契约给的是 position=50；
   漏斗顺序据此改为"精确 → 宽泛"，见 `tests_funnel.py`）
- ~~D17~~ 规则层对口语变体覆盖不全　**已修**（「我走了」「看个片」等缺失同义词补齐，离线 100%）
- ~~D18~~ **条件指令被立即执行**：`如果客厅没人了就把灯关掉吧` 曾被 4ms 内当成"立即关灯"
  执行。条件是"创建自动化规则"，不是"立刻动作"。已加条件句保护（同时出现条件词与
  结果词才判定），诚实拒绝而非静默执行　**已修**
- ~~（性能）~~ Manager 逐子任务各调一次 LLM（`1+N` 次）→ 改为批量翻译（`1+1` 次），
  实测 trace 已生效。**但总耗时未改善，瓶颈在模型生成速度**，见上方硬指标说明

待处理：
- **D6** `root_agent.py` 设备正则仍偏宽（`if "空调" in text` 之类），已排在能力契约之后
  缓解，但根治靠 1b 的工具化改造 → 阶段 1b
- **D7** 租约参数包络未实现；`_execute` 用 `is_expired` 绕过版本校验 → 阶段 2
- **D9** `.env` 含真实智谱 API Key，**需轮换**（属红线操作，须用户确认后执行）→ 待办
- **D19** `LLM_TIMEOUT=30` 让超时保护形同虚设；需由 `llm_budget.py` 统一约束墙钟预算 → 阶段 1b
- **D20** 两个 system prompt 合计约 2.8k token（Manager 把 17 台设备的属性与命令全量塞进
  prompt）。根治方案是元工具化（`list_devices` 只回精简行、`list_commands(id)` 按需拉）
  → 阶段 1b
