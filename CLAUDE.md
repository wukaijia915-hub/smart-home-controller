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
│   │   ├── manager_agent.py   Manager：校验+执行（最后一道闸门，**不持有 LLM**）
│   │   ├── plan.py            SubTask / TaskPlan（中立模块，避免循环导入）
│   │   └── llm_budget.py      ★ LLM 调用预算（次数/token/墙钟）
│   ├── toolkit/             ★ 工具层（LLM 影响世界的唯一出口）
│   │   ├── schema_gen.py      从 Device.attributes 反推 schema，永不漂移
│   │   ├── registry.py        ToolRegistry（fail closed：未装护栏则拒绝执行）
│   │   └── guard.py           ManagerGuard：工具层↔护栏层的唯一接缝
│   ├── perception/          ★ 感知层
│   │   ├── fusion.py          多模态置信度融合（移植心镜，二模态→N 模态）
│   │   ├── modal_gate.py      MultiModalGate：融合结果包成统一门控结果
│   │   ├── situation.py       白盒情境参数（5 个，每个带真值来源与原始依据）
│   │   ├── service.py         感知层入口：模态 → 融合 → 门控 → 询问式建议
│   │   ├── gpu.py             算力后端探测（如实标注：当前无热路径，不是成果）
│   │   └── modalities/        各模态检测器（视觉行为/环境行为/面部/前庭）
│   ├── memory/              ★ 记忆层三源（只读世界，不写世界）
│   │   ├── habits.py          习惯统计 + 显式偏好一致性校验
│   │   ├── manuals.py         设备手册检索（含共用打分函数）
│   │   └── experience.py      经验复用（成功案例回灌 prompt，确定性排序）
│   ├── observability/       ★ 可观测性（全部加锁）
│   │   ├── rate_limit.py      IP 滑窗限流（普通 300/min，LLM 10/min）
│   │   ├── api_log.py         API 统计
│   │   └── llm_log.py         LLM 调用日志 + 决策分层延迟（/api/metrics/llm 的取数来源）
│   ├── jobs/daily_summary.py ★ 外环：22:00 家庭日报（LLM 优先 / 模板降级）
│   ├── alerts.py            ★ 内环告警：读数 → 声明式阈值 → 结构化事件
│   ├── lease.py             授权租约 + 高危策略 + LeaseGate
│   ├── state.py             单一真相源：版本 + 审计日志 + pending 队列
│   ├── capabilities.py      能力契约 + 别名归一 + 孪生配对
│   ├── event_routes.py      声明式事件路由（高危动作默认拒绝）
│   ├── edge_cloud.py        端云隐私分级 L0/L1/L2
│   ├── devices/             设备抽象 + 虚拟设备工厂
│   ├── config/              JSON 配置（devices/scenes/environment/capabilities/
│   │                         event_routes/alert_routes/situation_mapping/loops）
│   └── knowledge/           兼容垫片 → 转发到 memory/（不要再依赖）
├── data/                    ★ 运行时状态（习惯/经验/日报/定时任务/日志），已 gitignore
├── frontend/                原生 JS + Three.js，无构建工具
├── experiments/             ★ 方案选型的实测依据（含被否决的路线 + bench_collect.py）
├── docs/                    ★ 设计文档（架构图/BENCH/白皮书/选型论证/感知层可靠性/答辩Q&A）
├── tests_*.py               根目录的回归测试脚本（**12 个**，见第四节）
└── requirements.txt
```

> **本目录树只列实际存在的文件。** 曾出现过「树里写着 `react_loop.py` / `views.py` /
> `meta_tools.py`、仓库里却没有」的情况 —— 那是照抄方案文档留下的漂移，会让读的人
> （以及后续的 AI 助手）去找不存在的文件。方案里**规划但未实施**的模块（如 ReAct 循环，
> 已实测否决）写进 `docs/` 与 `.md` 正文的 Future Work，**不写进本目录树**。
> 改动目录结构时，顺手核对这一段。

**放哪里的规则**
- 新能力 → 先问「这是感知、决策、工具、护栏、记忆、观测中的哪一层」，放对应目录，不要塞进 `main.py`
- 新配置 → `backend/config/*.json`，**不要硬编码进 Python**
- 新文档 → `docs/`，命名用中文短语
- 临时脚本/调试产物 → **不要提交**，用完删

**运行时状态一律进 `data/`**（`data/` 已 gitignore）。代码目录只放代码与静态知识资产
（`backend/knowledge/device_manuals.json` 是随仓库提交的内容，属于后者）。
可用 `LINGXI_DATA_DIR` 覆盖数据目录 —— **回归测试必须覆盖它**，理由见 5.4：
测试曾经直接写真实用户数据，累积出「用户最常用场景 home（使用 1842 次）」这种纯噪声结论。

---

## 三、硬指标（改动后必须复测，不可退化）

| 指标 | 当前实测 | 红线 |
|---|---|---|
| 规则层响应延迟 | 平均 **0.13ms**，p95 0.26ms | **平均 <10ms，p95 <100ms** |
| 设备控制成功率 | 64/64 = 100% | **100%** |
| 决策准确率（标准场景集 S1-S11） | 离线 100%（90/90） | **≥90%** |
| 前端渲染帧率 | 56 FPS（待浏览器复测） | ≥30 FPS |
| 慢环 LLM 调用次数 | 规则命中 0 次 / 走慢环 1 次 | **慢环 ≤1 次**（Manager 不调 LLM） |
| 慢环端到端延迟 | 8–13s（未达标） | P95 <5s（**当前服务商下达不到，见下**） |
| 慢环降级率 | 见 `/api/metrics/llm` | 无红线，但**必须可见**（降级要打 trace 与计数） |

**分层延迟必须分开报，不要合成一个「平均响应时间」**。规则层是纯内存查表
（毫秒级），慢环是模型生成（秒级），合成一个数字既不能解释性能也不能解释体验。
取数一律走 `GET /api/metrics/llm`（`observability/llm_log.py` 记录真实决策样本），
不要在文档里手填一个好看的数字 —— 评委可以当场调这个接口对。

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
- 规则层 P50 0.11ms —— 确定性、无外部依赖、覆盖标准场景集 100%（90/90）
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

# 全量门禁（每个阶段收尾必须 12 脚本全绿）
# 建议加 LINGXI_DATA_DIR 指向临时目录，避免测试写进真实用户数据
export LINGXI_DATA_DIR=$(mktemp -d)
LLM_OFFLINE=1 python tests_smoke.py \
  && LLM_OFFLINE=1 python tests_agents.py \
  && LLM_OFFLINE=1 python tests_capabilities.py \
  && LLM_OFFLINE=1 python tests_edge_cloud.py \
  && LLM_OFFLINE=1 python tests_error.py \
  && LLM_OFFLINE=1 python tests_funnel.py \
  && LLM_OFFLINE=1 python tests_budget.py \
  && LLM_OFFLINE=1 python tests_perception.py \
  && LLM_OFFLINE=1 python tests_memory.py \
  && LLM_OFFLINE=1 python tests_observability.py \
  && LLM_OFFLINE=1 python tests_loops.py \
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
- **感知层不得产生 lease**、**记忆层不得产生设备动作**——见下方红线
- **外环（日报）失败不得影响内环**。`jobs/daily_summary.run()` 遇任何异常返回
  `ok=False` 而不抛出去（它跑在后台任务里，抛出去会静默杀掉那个循环）

**红线（代码级不可违背）**

1. **感知结果绝不进入控制回路**。`PERCEPTION_CANNOT_ACTUATE = True`。融合输出只能生成**询问式**话术（"要不要把灯调暖一点？"），绝不自动执行设备动作。`tests_perception.py` 第 7 节会**静态扫描** `backend/perception/` 下所有 .py，一旦出现 `registry.control(`、`Lease.issue`、或导入 DeviceRegistry/lease，测试即失败 —— 这条承诺靠代码评审守不住。
2. **LLM 绝不直连设备**。LLM 只能通过 `toolkit` 的工具影响世界，且每个副作用工具内部强制走 `ActuationGuard`。
3. **原始画面/音频绝不出家庭**。视觉链路一律端侧推理，只把结构化标签上云，复用 `edge_cloud` 的 L0 分级。
4. **不做情绪测量**。面部表情等信号标注为**演示级**，如实报告验证边界，不做准确率承诺。
5. **规则层不得引入 LLM**（见第三节）。
6. **记忆层只能影响 prompt，不能影响世界**。`backend/memory/` 三个源都不得导入
   `DeviceRegistry` / `lease`，不得出现 `registry.control`。`tests_memory.py` 第 4 节
   静态扫描（AST + 去注释/去 docstring，避免"谁解释谁违规"）。理由与红线 1 相同：
   半年后有人加一句"自动应用学到的偏好"谁也不会注意。
7. **事件路由不得自动执行高危动作**。事件路由是**无人确认**的路径（没有弹窗、
   没有人点「确认执行」）。`event_routes._refuse_high_risk()` 默认拒绝，
   需要自动执行的路由必须在配置里显式写 `"allow_high_risk": true` ——
   让这个决定被写下来、被评审看到，而不是悄悄发生。

**⚠️ 一个必须能当场分辨的区别：内环告警可以自动开空调，感知层不可以**

| | 内环告警（`alerts.py`） | 感知层（`perception/`） |
|---|---|---|
| 输入 | 传感器读数（物理量直接比较） | 多模态**统计推断**（置信度融合） |
| 判定 | `temp_indoor > 30`，确定性 | 融合置信度 + 情境分类 |
| 阈值所在 | `config/alert_routes.json`，可复核 | 权重/区间在配置，但结论是推断 |
| 失效模式 | 传感器坏了 → 读数明显离谱 | 不透明（单模态误判、模态冲突） |
| 能否触发设备动作 | **能**（走 EventRouter，可审计） | **绝不能**（红线 1） |

二者外观相似、性质不同。**答辩必问**："既然环境告警能自动开空调，为什么情绪判断
不能自动调灯？" 答不上来这一页，红线 1 就会显得像自相矛盾而不是设计约束。

**其他**
- 测试/估算不出真值时，**如实说明边界**，不要用形容词代替数据（"大幅提升"→ 给数字）
- 不要为了让代码跑起来注释掉报错或加绕过标记，找根本原因
- 密钥、token、密码不进代码、不进 commit、不进日志。`.env` 已在 `.gitignore`，**永不提交**
- 新增第三方依赖前先问：能否用标准库？`requirements.txt` 的"5 行、pip install 即跑"是核心卖点，**重型依赖（如 TensorFlow）必须放 `requirements-vision.txt` 并保证未安装时优雅降级**

### 5.4 写测试时要避开的三个坑（都已实测踩过，各有守门测试）

1. **不要让断言取决于跑测试的钟点**。`late_night_activity` 规则在 23:00–05:00 生效，
   而服务层测试原本用 `datetime.now()`：同一份代码 22:00 跑是绿的、23:01 跑就红。
   凡涉及时间/时段的判定，**显式传 `now`**（`PerceptionContext.now`、
   `SituationInput.now`、`/api/perception/observe` 的 `now_hour`）。
2. **不要写死配置里的条数**。`tests_capabilities.py` 曾断言"被忽略的路由数 == 3"，
   阶段 4 往配置里加了 7 条环境联动后它就假失败。断言性质（"没有任何路由命中"），
   不要断言配置的当前规模。
3. **不要让测试写真实用户数据**。测试必须设 `LINGXI_DATA_DIR` 到临时目录。
   曾经的后果：`user_habits.json` 累积几千次测试调用，界面上显示
   「用户最常用场景 home（使用 1842 次）」—— 那是纯测试噪声，不是用户行为。

---

## 六、当前阶段

- ✅ 阶段 0　修地基（commit `6b37794`）
- ✅ 阶段 1a 五级漏斗接线（技能 + 能力契约接入主链路、条件句保护）
- ✅ 阶段 1b **Design B：LLM 单次提议 + Manager 纯护栏**（含四项实测实验）
- ✅ `llm_budget.py` 慢环预算（次数/墙钟/单次超时 + 降级可见）
- ✅ 阶段 3　**感知层**：Gate 抽象 / 多模态置信度融合 / 统一门控 / 四个模态 /
  白盒情境参数 / 算力后端探测 / 感知端点 / 前端情境看板
  （算力后端当前**无热路径**，不得作为成果主张 —— `gpu.status()` 会明确警告）
- ✅ 阶段 4　**记忆与快慢双环**：记忆三源（手册/习惯/经验）/ 可观测性三件套 /
  外环 22:00 日报（LLM 优先模板降级）/ 声明式内环告警 / 环境联动 / 前端「记忆与观测」面板
- ✅ 阶段 5　**文档与竞赛资产**：`docs/` 六份设计文档 + `experiments/bench_collect.py`
  + `demo_fusion.py`（10 个演示点）+ 挑战杯三份文档同步至 V3.0 + `NOTICE` 逐项登记心镜来源
- ⏳ 阶段 2　租约包络与 `verify` 全路径（与阶段 3/4/5 改动文件不重叠，可穿插做）
- ⏳ 待办　接入真实视觉模型并补混淆矩阵（感知层优先级最高的一项）

**文档之间的引用关系（改文档时不要制造副本）**

| 想写的内容 | 写在哪 | 谁引用它 |
|---|---|---|
| 架构图 | `docs/架构图.md`（**单一出处**） | 白皮书 / 挑战杯方案 / HTML 架构图 |
| 性能数字 | `docs/BENCH.md`（由 `bench_collect.py` 产出） | 挑战杯方案 §八 / README |
| 感知层的验证边界 | `docs/感知层可靠性报告.md` | 白皮书 §五 / 挑战杯方案 §八补二 |
| 选型否决记录 | `experiments/README.md` | 白皮书 / `docs/为什么不用LangChain.md` |
| 答辩话术 | `docs/答辩Q&A预案.md` | 无（终端文档） |

**不要把架构图或性能数字复制到第二个文件里** —— 复制出来的副本会各自漂移，
最后没有人知道哪份是真的。

**阶段 4 三条必须记住的边界**（写进申报书前先读这里）

1. **叫「基于成功案例的 prompt 级经验复用」，不叫「自进化 / 自学习」。**
   没有梯度更新、没有策略迭代，只是把历史成功案例注入 prompt。
   `experience.py` 的 `rating` 默认**由执行结果推导**（全成功=5 / 部分=3 / 全失败=1），
   它衡量"这轮规划**执行**成功了吗"，**不是"用户满意吗"**；满意度只能靠
   `POST /api/memory/feedback` 的显式反馈，字段 `rating_source` 如实区分两者。
2. **心镜的 `random.sample` 必须不要照搬**。它对候选随机抽样，导致同一 category
   每次注入不同案例 —— 输入相同、注入不同，**"自进化有没有用"这个问题在那种实现下
   无法被验证**。本项目用确定性排序 `rating*2 + 关键词重合度`，
   `tests_memory.py` 有守门断言：同样输入两次检索结果必须完全相同。
3. **日报里"今日动作数"是下界，不是总数**。`StateStore.audit_log` 是环形缓冲
   （上限 `store.max_log`，现为 2000），超出部分已被丢弃。返回体
   `facts.actions.note` 会如实标注这一点，**不要**在文档里把它写成"今天执行了 N 次操作"。

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
- ~~D19~~ `LLM_TIMEOUT=30` 超时保护形同虚设　**已在代码层兜住**
  `agents/llm_budget.py` 用 `per_call_timeout`（默认 15s，可被 `LLM_CALL_TIMEOUT`
  覆盖）压过后端默认值，并给一次 decide() 加上调用次数与墙钟双重上限。
  `.env` 那个 30s 建议顺手改掉，但**不再是安全缺口**
- ~~D20~~ Manager 把 17 台设备全量塞进 prompt　**已消除**（Manager 不再持有 LLM，
  其 prompt 整个删除）。注意：Root 的 prompt **保留**完整设备清单是正确的 ——
  实测 prompt 体积不影响延迟（`experiments/README.md` 实验五），删它没有收益
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

阶段 4 已修复（记忆与快慢双环）：
- ~~D23~~ **事件路由可自动执行高危动作（高危漏洞）**　**已修**
  事件路由是**无人确认**的路径（无弹窗、无人点确认），但它此前直接调
  `registry.control()`，无租约、无版本校验、无高危确认。只要有人往
  `event_routes.json` 加一条指向门锁/燃气灶的联动，门锁就会在无人值守时被打开。
  **与「高危动作必须二次确认」的宣称直接矛盾。**
  修法：`_refuse_high_risk()` 默认拒绝，需自动执行必须显式写
  `"allow_high_risk": true`（决定被写下来、被评审看到）。
  `tests_loops.py` 第 2 节是它的守门测试，且**先断言"该联动确实被路由命中"** ——
  否则"什么都没发生"会被误读成"高危被拒绝了"（写这条测试时真的踩到过：
  直接塞 `router.raw` 会被 `route()` 的热重载覆盖，得到空动作列表的假通过）
- ~~D24~~ **测试污染真实用户数据**　**已修**
  测试直接写 `backend/knowledge/user_habits.json`，累积几千次调用后
  `total_commands` 达到 11046、场景 home 达 1842 次。界面上会把这些**测试噪声**
  显示成"用户习惯"。修法：运行时状态统一进 `data/`，测试用 `LINGXI_DATA_DIR`
  指向临时目录；旧数据有一次性的迁移读取（**只在规范目录发生**，临时目录不迁移，
  否则测试会导入生产数据、断言不可复现）。
- ~~D25~~ **测试断言取决于跑测试的钟点**　**已修**
  `tests_perception.py` 服务层用例用 `datetime.now()`，而 `late_night_activity`
  规则在 23:00–05:00 生效 —— 同一份代码 22:00 绿、23:01 红（实测复现）。
  修法：显式钉死 `now`，并补一对断言（同样输入换到下午就不再判为 anomaly）
  作为"时间必须可注入"的守门测试。`/api/perception/observe` 加 `now_hour`，
  演示时不必等到深夜也能复现该场景。
- ~~（测试）~~ `tests_capabilities.py` 断言"被忽略的路由数 == 3"**已修**
  （改成对配置里路由总数取数）—— 加了 7 条环境联动路由后它假失败
- ~~（一致性）~~ `EventRouter.route()` 对多动作 fan-out 一律并行 **已修**
  同一台设备上的多条动作现在串行（`same_device` 判定），否则
  「turn_on 再 set_temperature」可能颠倒，回读证据链会出现瞬时不一致

阶段 5 已修复（文档与竞赛资产）：
- ~~D27~~ **规则层 98% 的延迟是一处磁盘 I/O**　**已修**
  `memory/habits._save()`（序列化整个 JSON + 写盘 + 原子替换）被 `decide()` 对
  **每一条成功执行的动作**调用 —— 一次 4 动作的场景就是 4 次全文件重写。
  实测：完整 `decide()` 8.74ms，漏斗解析 0.014ms，**屏蔽习惯落盘后 0.13ms**。
  修法：`record_*` 只置脏标记，由内环 2 秒一跳统一 `flush_if_dirty()`，
  lifespan 退出时补一次。**规则层 avg 8.46ms → 0.132ms（64×）**。
  详见 `docs/BENCH.md` §2.1。
  > **这条的教训是"先量再改"**：发现它是因为写文档时看到旧报告记 3.38ms、
  > 新采集记 8.46ms，**同机同输入差 2.5 倍**。与其猜，不如把
  > `_save` 换成空函数量一次 —— 差值立刻定位。
  > 顺带说明：旧报告的 3.38ms 同样含这项开销（当时文件更小），
  > 所以**两版数字都掺了同一个杂质**，只是占比不同。

待处理：
- **D6** `root_agent.py` 设备正则仍偏宽（`if "空调" in text` 之类），已排在能力契约之后
  缓解，但未根治 → 阶段 2 前评估是否值得继续投入
- **D7** 租约参数包络未实现；`_execute` 用 `is_expired` 绕过版本校验 → 阶段 2
- **D9** `.env` 含真实智谱 API Key，**需轮换**（属红线操作，须用户确认后执行）→ 待办
- **D26** 记忆层的偏好一致性校验只覆盖温度/亮度两个键（`_OBSERVED_ATTR`）。
  够用且诚实（没有推断，只有这两类有稳定的"声明值 vs 观察值"对应），
  但不要在文档里写成"支持全部偏好类型"
- **D28** 习惯数据改为延迟落盘后，**进程被强杀（非正常退出）会丢掉最多 2 秒的计数**。
  这是有意的取舍（习惯统计不是事务数据），但不要在文档里写成"零丢失"
