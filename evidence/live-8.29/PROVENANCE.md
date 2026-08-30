# evidence/live-8.29/ —— 真实运行（有 LLM）链路证据 · 来源说明

本目录是 **2026-08-29 晚 Demo 视频录制现场**的真实运行证据，与包内其他证据物理隔离：

- `runs/`（包根）与 `evidence/sample_run/`：**无 LLM 的确定性回放**产物（`scripts/eval_replay.py` / `local_dryrun.py` 驱动，可逐位复算）。
- **本目录**：**有 LLM 的真实多 Agent 协作**产物——AgentTeams 平台上 Worker + TeamLeader 真实协作，LLM 做判断、网关确定性代码做裁决。

分层口径的唯一来源是包根 `EVIDENCE_HONESTY.md`；引用本目录任何数字前请先读它。

## 采集环境

- 平台：腾讯云 Cloud Studio（AgentTeams 部署环境），网关 notary_gateway 0.8.0（18090），Console v2（18091）
- AgentTeams：11 个 Worker 容器（清单见 `agentteams-platform/agt-workers-8.30.txt`）
- 录制时间：2026-08-29 20:51–22:11（服务器本地时间，含彩排）
- 回收时间：2026-08-30，按 `w7-cloudstudio-8.31.md` S8 流程分三轮回传（runs + pace 日志 + 容器日志 + Skill registry + gateway.log，经 ECS 中转）

## 当晚运行全序列（gateway.log 逐行互证）

| 时刻 | Run | 性质 | 终态 | postmortem 沉淀 |
|---|---|---|---|---|
| ~20:51 | mb_delivery_semantics | T1 彩排（run 目录被正式运行覆盖，gateway.log register 记录与 registry md 时间戳互证） | RELEASED | `mutation-dispute-escalation-risk` |
| 21:04–21:08 | **mb_delivery_semantics** | **T1 green 正式**（pace-t1） | RELEASED | —（skill.match/get 咨询） |
| 21:18–21:23 | **qb_external_subtle** | T2 彩排（对照镜头 T2-subtle-desk.webm） | REJECTED | `undeclared-side-effect-scan` |
| 21:36–21:40 | **qb_external_leak** | **T2 red 正式**（pace-t2：RECEIVED 21:37:22→REJECTED 21:38:23） | REJECTED | `resource-leak-success-path-scan` |
| 21:45–21:49 | **mb_external_breaking** | 补充演示（D1-R 真实溯源场景） | REJECTED | `bundled-api-breaking-scan` |
| 21:58–22:11 | **mb_delivery_semantics_ambiguous** | **T6 HITL 正式**（pace-t6，2 次 ESCALATED 人工回路、三次重入） | RELEASED | —（skill.match/get 咨询） |

加粗 = 留存完整 run 目录的 5 个 run（本目录 `runs/` 下）。当晚另有 `skill-showcase` 的 6 次 match 调用（127.0.0.1，S6-B Skill trace 演示镜头，走网关 runless 分支，不产生 run 目录）。

## 三个正式 Run（与 Demo 视频逐段对应）

| Run | 视频段 | 墙钟（pace 日志） | 终态 | 说明 |
|---|---|---|---|---|
| `runs/mb_delivery_semantics` | T1 green | 21:04:18–21:08:23（4m05s） | RELEASED | 完整九状态链，门禁全绿（test 25/25、mutation 1.00、convention 绿），公证书 frozen_hash 前缀 b2ddfa7a |
| `runs/qb_external_leak` | T2 red | 21:37:22–21:38:23（1m01s） | REJECTED | 外部送审变更（真实原型 openai/openai-python #2708：成功路径句柄泄漏）在 triage 段拒止，三门禁未执行；postmortem 沉淀 `resource-leak-success-path-scan` |
| `runs/mb_delivery_semantics_ambiguous` | T6 HITL | 21:58:14–22:10:48（12m34s） | RELEASED | 两次 ESCALATED 人工回路（21:58:56、22:06:03），人工等待合计 3m03s；保留的 run 目录对应最终通过段（trace 起点即第三段 22:06:58），前两段升级尝试的 run 数据被重入覆盖，pace 日志为升级链路的留存记录；公证书 frozen_hash 前缀 158e41cd |

每个 run 目录内含：`trace.jsonl`（每次调用含 ts/run_id/tool/role/payload_sha256/result_sha256）、`contract.json`、`verdicts/`、`manifest.json`（SHA-256 封印清单）、`certificate.md`、`checkpoint.json`、`metrics.json`（取证后由 `scripts/trace_metrics.py` 聚合生成，为派生文件）。

## 视频对账备注（T2 段）

成片 T2 段实况画面（T2-leak-console.webm，Console 窗口 21:36–21:39）与 `qb_external_leak` 正式 run、pace-t2 窗口三方吻合。T2 红结果页定格帧（T2-L1-redcard-desk.webm）画面可见编号为 **`zz_l1_replay_leak`**——那是同一 leak 故事的**无 LLM 确定性重放** run（录制后已清理，trace 哈希链前缀 e8785fe7、时间戳为重放保留的原始时刻）：否决文案（成功路径资源泄漏）与正式 run 一致，但按角标制度该帧属 REPLAY 素材，评审对账时请以此备注为准：画面编号 zz_l1_replay_leak ↔ 正式 live run `qb_external_leak`（pace-t2 窗口 21:37–21:38，trace 可互证）。对照镜头 T2-subtle-desk.webm（编号 qb_external_subtle）为彩排 run 画面。

## 目录内容

- `runs/` —— 5 个真实 run 的完整证据目录（仅移除可再生成的 Python `__pycache__` 字节码缓存，并追加派生文件 metrics.json，其余逐字节保持采集原样）
- `pace-logs/` —— 起搏器逐状态墙钟日志（15-B 协同层指标的原始数据）
- `skill-registry/` —— 取证时 Cloud Studio 的 Skill registry 全量（8 个 md + index.json）。其中 4 个 md（always-true-guard-scan / compound-boundary-defect-scan / delivery-semantics-review / idempotent-retry-design）为包基线回放沉淀；另 4 个（mutation-dispute-escalation-risk 20:51 / undeclared-side-effect-scan 21:22 / resource-leak-success-path-scan 21:38 / bundled-api-breaking-scan 21:49）为**当晚 live 沉淀**，文件时间戳与 gateway.log 的 4 次 register 200 响应逐一对上。
- `agentteams-platform/` —— 平台层证据：worker 容器清单 + 11 个 Worker 容器日志（`docker logs --since 72h`）+ `gateway.log`（网关访问日志，当晚全序列的对账主线）

## 可复核方式

1. 封印校验：对每个 run，`manifest.json` 列出全部产物的 SHA-256，可重算比对。
2. 视频对照：trace/公证书中的 run_id 与状态时间线，与视频画面（含 LIVE/REPLAY 角标）逐段对应；T2 红卡帧的编号对账见上方备注。
3. 口径对照：本目录只声明"真实运行"，所有评测数字（35/35、41/41 等）仅来自无 LLM 回放层，见 `EVALUATION.md`。
