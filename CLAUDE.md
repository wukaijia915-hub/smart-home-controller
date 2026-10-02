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
├── experiments/             ★ 方案选型的实测依据（读 README.md，含被否决的路线）
├── docs/                    ★ 设计文档（架构图/BENCH/白皮书/选型论证）
├── tests_*.py               根目录的回归测试脚本（7 个，见第四节）
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
| 慢环 LLM 调用次数 | 规则命中 0 次 / 走慢环 1 次 | **慢环 ≤1 次**（Manager 不调 LLM） |
| 慢环端到端延迟 | 8–13s（未达标） | P95 <5s（**当前服务商下达不到，见下**） |

**关于决策准确率**：S1-S11 是回归套件，规则层应覆盖 100%（现已达到）。LLM 的价值体现在
套件之外的模糊/多步指令，由 `tests_performance.py --api` 单独测量。不要笼统宣称
"LLM 决策准确率 100%" —— 评委问「把 API Key 删掉再跑一遍」就会穿帮。

**关于慢环延迟（2026-10-02 实测，重要）**

结论：**延迟 ≈ 1.2s 基线 + 输出 token 数 ÷ 15**。模型输出速率约 14–16 tok/s，
是唯一的硬约束。已用对照实验确认（见 `experiments/README.md` 实验五）：

| 变动 | 结果 |
|---|---|
| prompt 51 → 1659 token（33×），输出固定 | 3551ms → 1800ms，**没有增加** |
| 输出 13 → 196 token（15×），prompt 固定 | 1277ms → **11978ms，线性增长** |

**所以不要在这两件事上浪费时间，它们都不降低延迟**：
- ❌ **重构管线减少调用次数**。已实测三条路线：Manager 批量翻译（模型无法可靠产出
  多项嵌套响应，8 次探测全畸形）、Root 直接输出动作（可行且幻觉率 0%，但单次输出
  变大，总耗时相当）、工具元化压缩 prompt（prompt 体积本就不影响延迟）
- ❌ **压缩 prompt 体积**。只省成本，不省时间

**有效的只有两条**：减少输出 token（少提议几个动作），或换更快的模型 / 本地推理。

正确表述（不要声称"慢环 P95 <5s"）：
- 规则层 P50 7.4ms —— 确定性、无外部依赖、覆盖标准场景集 100%（90/90）
- 慢环单次输出约 150–250 token，实测 8–13s；走预算化降级，P95 有界
- 换更快模型可线性改善

另注：`.env` 里 `LLM_TIMEOUT=30`，超时保护形同虚设；建议下调到 10s 或由
`llm_budget` 统一约束（阶段 1b）。

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
- **`ManagerAgent` 不持有、不调用 LLM**。它是纯护栏：校验 / 授权 / 执行 / 记录。
  Root 已在慢环给出具体动作，Manager 不需要也无法再"翻译"一次。
  `tests_funnel.py` 第 6 节把这条钉住了。**若将来有人想在 Manager 里加 LLM 调用，
  先读 `experiments/README.md` 实验三**（那条路实测不可靠，且不降延迟）
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

- ✅ 阶段 0　修地基（commit `6b37794`）
- ✅ 阶段 1a 五级漏斗接线（技能 + 能力契约接入主链路、条件句保护）
- ✅ 阶段 1b **Design B：LLM 单次提议 + Manager 纯护栏**（含四项实测实验）
- ⏳ 阶段 2　租约包络与 `verify` 全路径
- ⏳ 阶段 3　感知层（多模态置信度融合 / 白盒情境参数 / 国产算力链）
- ⏳ 阶段 4　记忆三源与快慢双环
- ⏳ 阶段 5　文档与竞赛资产

完整方案：`C:\Users\Lenovo\.claude\plans\c-users-lenovo-desktop-aigc5-1-xinjing-federated-perlis.md`
实验依据：`experiments/README.md`（**改动决策前先看它**，避免重复已被否决的路线）

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

阶段 1b 已修复（Design B：LLM 单次提议 + Manager 纯护栏）：
- ~~D19~~ `LLM_TIMEOUT=30` 超时保护形同虚设 → 仍在 `.env`，但**已加 `llm_budget` 之前在
  代码层留痕**；建议一并下调到 10s（属红线操作，见 D9）
- ~~D20~~ Manager 把 17 台设备全量塞进 prompt　**已消除**（Manager 不再持有 LLM，
  其 prompt 整个删除；Root 的 prompt 保留了完整设备清单，因为实测 prompt 体积不影响延迟）
- ~~D21~~ **模型端点继承 Windows 系统代理导致 LLM 层无声消失**　**已修**
  （`resolve_proxies()` 默认直连 + 三处静默降级告警）
- ~~（架构）~~ Root 输出高层目标、Manager 二次翻译（LLM 调用数 `1+N`）→ 改为
  **Root 单次输出具体动作、Manager 零 LLM**（`1` 次）。实测幻觉率 0%（0/25），
  依据见 `experiments/README.md`
- ~~D22~~ **工具层完全绕过护栏（高危漏洞）**　**已修**
  `/api/tools/call` 与 MCP `tools/call` 此前直接调 `registry.control()`，
  无租约、无版本校验、无高危确认。实测可直接 `lock_entry_01__unlock`（门锁被解锁）
  与 `gas_stove_kitchen_01__turn_on`（燃气灶被点燃），与"授权租约是防越权的核心护栏"
  的宣称直接矛盾。
  修法不是另写一套校验，而是 **`ManagerGuard` 把工具调用包成单动作 `TaskPlan`
  交给同一个 ManagerAgent** —— 对话路径与工具路径共用同一条链，无双份语义。
  且护栏**未装配时拒绝执行**（fail closed），避免漏洞以后悄悄回来

**决策变更说明**：原方案的阶段 1b 是「Root 慢环改 prompt-based ReAct 工具循环」。
经实验否决（`experiments/README.md` 实验一：格式遵循度 80%，且延迟无优势），
改为上述 Design B。ReAct 写入 Future Work 并附实测数据作为不采用的理由 ——
在申报书里这属于"实证判断力"，比堆机制更有说服力。

待处理：
- **D6** `root_agent.py` 设备正则仍偏宽（`if "空调" in text` 之类），已排在能力契约之后
  缓解，但未根治 → 阶段 2 前评估是否值得继续投入
- **D7** 租约参数包络未实现；`_execute` 用 `is_expired` 绕过版本校验 → 阶段 2
- **D9** `.env` 含真实智谱 API Key，**需轮换**（属红线操作，须用户确认后执行）→ 待办
