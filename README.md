# 犀鉴 — 基于 Wi-Fi CSI 的隐私优先家庭感知与可信自适应系统

> **更安全、更智能的智能家居系统。**
> 前身：灵犀 · 门控式大模型家居中枢。v2.0 按 2027 广东攀登计划申报书重构。

犀鉴不做"再接入一个设备"，而是补家庭自动化真正缺的那一层：**这一间房此刻有没有人、
该不该动作、动作出错后能不能收回**。射频前端复用成熟的 Wi-Fi CSI 开源基线，
创新集中在它没有覆盖的上层——全屋房间融合、三态判定、六路旁证、信任层与 L5 目标级自主。

---

## 一、两根支柱与八项特点

| 支柱 | 特点 | 一句话 |
|---|---|---|
| 安全 | ① 隐私来自部署位置 | 不采图像/声音，卧室、卫浴、更衣区也能自动化 |
| 安全 | ③ 不误报 | 单源不得判无人；判不准就输出"不确定"，不硬猜 |
| 安全 | ④ 能解释、可撤销 | 六段因果链；可逆动作 10 分钟内一键撤销；高危永不自动执行 |
| 智能 | ⑤ L5 目标级自主 | 按能力属性学偏好带，多设备围绕同一目标协商 |
| 感知 | ② 从单点做到房间、从房间做到全屋 | 多节点融合 + 冲突归因 + 掉线降级 |
| 工程 | ⑥ 成本可部署 | 每房间物料 ≤100 元，复用家里 2.4GHz 网络 |
| 工程 | ⑦ 自研边界清晰 | 射频基线只做采集与单点运动；四层上层自研，NOTICE 逐项登记 |
| 工程 | ⑧ 工程化底座 | 门控执行链、统一 Gate、端云分级、可观测性、15 个回归脚本 |

## 二、五层架构

```
┌──────────────────────────────────────────────────────────────┐
│ ⑤ L5 目标级自主   偏好带 / 目标仲裁 / 四道闸 / 冷启动 / 漂移降级 │
├──────────────────────────────────────────────────────────────┤
│ ④ 信任层          六段因果链 / 10 分钟撤销 / 高危拒绝 / 失败归因 │
├──────────────────────────────────────────────────────────────┤
│ ③ 多源校验        六路旁证 → 冲突归因 → empty_confident 提升     │
├──────────────────────────────────────────────────────────────┤
│ ② 房间融合        节点状态机 → 房间四态 → 降级语义               │
├──────────────────────────────────────────────────────────────┤
│ ① 射频基线        ESPectre 独立运行，只经 MQTT/HTTP 上报事件     │
└──────────────────────────────────────────────────────────────┘
        ▲ 浏览器：3D 孪生 / 房间视图 / 信任面板 / 自治面板
```

**许可证边界**：ESPectre 为 GPL-3.0-only，本项目为 Apache-2.0；二者通过 MQTT/HTTP
进程边界通信，本仓库不含其代码/固件（见 [NOTICE](NOTICE)）。

## 三、房间三态：三态判定，四值实现

| 状态 | 含义 | 允许的动作 |
|---|---|---|
| `present_confident` | 有新鲜运动事件 | 可逆动作 |
| `empty_confident` | CSI 无运动 + **≥2 路旁证一致** + 持续 N 分钟 | 可逆动作（关灯/关空调/扫地机进入） |
| `uncertain` | 数据不足、旁证冲突、或"idle 够久但旁证未到" | **不执行关灯/关空调**，只出建议 |
| `degraded` | 节点掉线/无数据 | 只下调置信度并告警，**绝不推断无人** |

六路旁证：**CSI、PIR、插座功率、设备操作日志、作息基线、门磁**。
除 CSI 外当前均为仿真量，证据里逐项标 `source_kind="simulated"`；接真实器件后
只换 `RoomService` 的取值来源，判定规则不变（见 [docs/三态判定与多源校验.md](docs/三态判定与多源校验.md)）。

冲突不丢弃，分类为：`human / pet / environment / fault / unknown`。

## 四、信任层：每条决策可查、可撤销

每条决策用同一个 `decision_id` 串起：

```
signal → inference → gate → lease → action → readback
```

- **失败归因**到具体一层：`sensor / inference / gate / lease / device / readback`；
- **可逆动作**保留前态，10 分钟内可撤销；撤销前做**漂移校验**（设备当前值必须仍等于 after，否则拒绝覆盖他人操作）；
- **高危动作**（门锁、燃气、摄像头直播）永不自动执行；事件路由需显式 `allow_high_risk`；
- 被撤销、被否决的动作同样留档，不静默删除。

## 五、L5 有界自适应：三免 + 四道闸

| 项 | 内容 |
|---|---|
| 三免 | 免确认（自动执行占比 ≥90%）、免配置（新房间 3 天内自举）、免调参（漂移 7 天内恢复） |
| 偏好带 | 按能力属性 × 房间 × 时段学习；样本 <12 只给区间，不给建议（冷启动） |
| 目标仲裁 | 舒适优先于节能、可逆优先于不可逆；同分按设备 id，结果可回放 |
| 四道闸 | 参数包络 / 最大步长 / 最小间隔 / 当日预算 |
| 降级 | `auto → confirm → suggest_only → off`；退让后清空反馈、回影子模式重走冷启动 |
| 安全 | 门锁、燃气、摄像头、窗户**永不参与**；照明与窗帘只作演示 |

主场景：空调、除湿机、热水器、扫地机（见 [docs/L5自治设计.md](docs/L5自治设计.md)）。

## 六、快速开始

```powershell
cd smart-home-controller
python -m pip install -r requirements.txt

# 可选：大模型（不配也能跑，规则层全功能）
copy .env.example .env

python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
# 打开 http://127.0.0.1:8000
```

Docker（单容器，非 root + healthcheck）：

```bash
cp .env.example .env
docker compose up -d --build
```

CSI 接入（可选，需要 ESPectre 节点与本地 broker）：

```bash
pip install -r requirements-csi.txt          # 只装 paho-mqtt
# backend/config/wifi_csi.json: enabled=true, mqtt.host/port 指向 broker
```

无硬件也能演示：`POST /api/csi/replay` 回放节点事件（见 `docs/部署指南.md`）。

## 七、五个可现场演示的场景

| 场景 | 操作 | 看点 |
|---|---|---|
| 2 房间联动 | `POST /api/rooms/tick` 或等 5s 环 | 客厅/卧室各自三态，证据逐项展开 |
| 节点掉线降级 | 停止喂该节点事件 > `stale_after_seconds` | 房间转 `degraded`，**绝不判无人** |
| 静坐不误关空调 | idle 299s vs 302s + 旁证 | 299s 仍 uncertain；302s 且旁证一致才 empty |
| 误动作撤销 | `GET/POST /api/trust/undo/{action_id}` | 10 分钟内恢复前态；漂移时拒绝 |
| L5 免确认 | `adaptation.json: enabled=true` + `POST /api/adaptation/tick` | 偏好带 → 仲裁 → 四道闸 → 自动执行 |

## 八、接口（增量）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/rooms` | 全部房间四态、置信度、证据来源、节点在线 |
| GET | `/api/rooms/{room_id}/timeline` | 房间状态变化时间线 |
| POST | `/api/rooms/tick` | 手动推进一轮房间评估（演示） |
| GET | `/api/csi/status` | CSI 接入状态（enabled/paho/MQTT/节点统计） |
| POST | `/api/csi/replay` | 回放节点事件（无硬件演示） |
| GET | `/api/trust/recent` | 最近决策 + 可撤销动作 |
| GET | `/api/trust/{decision_id}` | 六段链 + 失败归因（滚出缓冲返回 404+原因） |
| GET/POST | `/api/trust/undo/{action_id}` | 撤销预检 / 执行撤销 |
| GET | `/api/adaptation` | 偏好带、提议、四道闸、自治等级 |
| GET | `/api/adaptation/health` | 纠正率/自动执行占比/降级比例与阈值 |
| POST | `/api/adaptation/tick` | 手动推进一轮 L5 评估 |
| POST | `/api/adaptation/{id}/accept\|reject` | 接受/否决建议（写入训练信号） |
| GET | `/healthz` | 无认证存活探针（容器 healthcheck） |

原有接口（聊天、设备、场景、定时、感知、事件路由、记忆、观测、MCP）保持兼容。

## 九、目录结构

```
backend/
├── main.py                 装配 + 路由 + lifespan 四个后台环（2s/5s/15s/60s）
├── execution.py            ★ 唯一写入口（租约/包络/四道闸/审计/撤销记录）
├── ingest/                 ★ ESPectre 接入（MQTT 契约 / 节点生命周期）
├── perception/
│   ├── rooms.py            节点 → 房间四态
│   ├── corroboration.py    ★ 六路旁证 + 冲突归因
│   ├── room_service.py     ★ 接入/融合/旁证/事件编排
│   └── ...                 多模态融合 / 情境参数 / 门控
├── trust/                  ★ explain（六段链）/ undo（前态/逆动作/窗口/漂移）
├── adaptation/             ★ preference / objectives / policy / service
├── agents/                 Root 提议 → Manager 纯护栏
├── devices/                19 台虚拟设备（含热水器/除湿机）
├── config/                 设备/场景/房间/旁证/信任/自治/裁剪/草稿 全部 JSON
└── ...
frontend/                   原生 JS + Three.js（房间/信任/自治面板）
tests_*.py                  15 个回归脚本
run_tests.py                一条命令跑完 15 个（离线 + 临时数据 + UTF-8）
Dockerfile / docker-compose.yml
```

## 十、测试

```powershell
python run_tests.py          # 15 个脚本，约 20s
python run_tests.py --verbose
python run_tests.py --only tests_csi.py tests_trust.py tests_adaptation.py
```

| 脚本 | 覆盖点 | 项数 |
|---|---|---|
| tests_smoke | 设备层 + 规则决策冒烟 | — |
| tests_agents | 租约/幻觉拦截/回读证据/参数包络/单一执行入口 | 14 组 |
| tests_capabilities | 能力注册表 + 事件路由 + 执行契约 | 30 |
| tests_edge_cloud | 隐私分级 / 脱敏 / 端云路由 | 19 |
| tests_error | 异常与边界输入 | 26 |
| tests_funnel | 五级漏斗 + 工具裁剪 + 架构不变量 | 58 |
| tests_budget | 慢环预算 + 降级可见 | 25 |
| tests_perception | 融合/门控/情境参数/感知层硬边界 | 106 |
| tests_memory | 记忆三源 + 经验闭环 + 只读红线 | 66 |
| tests_observability | 限流/分层延迟/降级率 | 48 |
| tests_loops | 内环告警/日报降级/事件高危拒绝/草稿确认 | 86 |
| tests_performance | 量化验收指标 | — |
| tests_csi | 节点契约/四态/旁证/降级/房间服务 | 104 |
| tests_trust | 六段链/五层归因/四道闸/撤销/静态不变量 | 48 |
| tests_adaptation | 偏好带/仲裁/冷启动/降级/安全排除 | 28 |

## 十一、指标（实测与边界）

| 指标 | 当前 | 说明 |
|---|---|---|
| 规则层延迟 | avg **0.25ms** / p95 0.51ms | `tests_performance`，纯内存；设备 19 台后从 0.15 升至 0.25 |
| 设备控制成功率 | 100%（64/64） | 虚拟设备 |
| 决策准确率 S1–S11 | 100%（90/90） | 规则层，离线 |
| 慢环 | 1 次 LLM，8–13s | 预算化降级，**不声称 P95<5s** |
| 房间三态误报/漏报 | **待实测** | 需硬件与成对语料，当前不填数字 |
| L5 三项判据 | **待实测** | 自动执行占比/撤销率/漂移恢复需家庭部署 |

## 十二、诚实边界

- 设备是**虚拟的**（19 台），未接真实灯具/空调/门锁；
- 六路旁证里除 CSI 外是**仿真量**，证据标注 `simulated`；
- 感知层四模态仍是模拟数据，未接真实视觉模型；
- **不作**跌倒检测、人数统计、身份/步态识别、穿墙定位、医疗级呼吸心率承诺；
- 无登录/多用户，不能直接上公网；Docker 镜像未在真实盒子验证；
- 性能数字区分"官方口径 / 自采数据"，未实测的一律留空。

## 十三、文档

| 文档 | 内容 |
|---|---|
| [docs/犀鉴-五层架构与创新点.md](docs/犀鉴-五层架构与创新点.md) | 五层架构、两根支柱、自研边界、与竞品差异 |
| [docs/三态判定与多源校验.md](docs/三态判定与多源校验.md) | 三态语义、六路旁证、冲突归因、判据 |
| [docs/L5自治设计.md](docs/L5自治设计.md) | 偏好带、目标仲裁、四道闸、冷启动、降级 |
| [docs/安全与稳定性排查报告.md](docs/安全与稳定性排查报告.md) | 缺陷台账、修复、验证记录、不能声称什么 |
| [docs/数据流向说明.md](docs/数据流向说明.md) | L0/L1/L2、出网点穷举、CSI 原始数据不出户 |
| [docs/部署指南.md](docs/部署指南.md) | 本地/Docker、MQTT 接入、回放、排障 |
| [docs/凭据轮换清单.md](docs/凭据轮换清单.md) | 密钥自查与轮换 |
| [CLAUDE.md](CLAUDE.md) | 开发约定与红线（改代码前必读） |

## 十四、路线

- **P0/P1b**：真实 ESP32 节点 7 天基线 → 真实 MQTT 驱动房间状态；
- **P2**：成对同会话语料（空—静—空），三态混淆矩阵与每小时误报率；
- **P3**：20 个故障注入归因、5 类撤销真机验证；
- **P4**：3–5 户家庭 2–4 周部署，L5 三项判据成表；
- **P5**：论文、软著、挑战杯、开源发布与 NOTICE 复核。

---

MIT / Apache-2.0 见 [LICENSE](LICENSE)；第三方来源见 [NOTICE](NOTICE)。
