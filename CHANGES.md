# CHANGES — agentteams 代码包变更记录

## v1.5（复赛终版，2026-08-30）

相对 v1.4 为**纯增量**变更（replay 35/35 results 前缀仍 `e7314eb4`，fuzz 47/47 前缀 `fe71d30b`，与 v1.2–v1.4 逐位一致）。

### 真实运行证据（L3 层）

- 新增 `evidence/live-8.29/`：2026-08-29 Cloud Studio 录制现场的真实运行（有 LLM）证据——5 个留存 run（T1 green / T2 red / T6 HITL 正式 + 2 个补充 run）、pace 起搏器日志、Skill registry 全量（含当晚 live 沉淀的 4 个 Skill）、AgentTeams 平台层日志（11 Worker 容器 + gateway.log）；PROVENANCE.md 固化采集来源、当晚运行全序列与视频逐帧对账（含 T2 红卡帧 replay 编号 zz_l1_replay_leak ↔ 正式 run qb_external_leak 的对账备注）。与包根 `runs/`（无 LLM 回放产物）物理隔离。
- `EVALUATION.md` §6 由占位转为实数：`scripts/eval_report.py` 新增 live_section，从 `evidence/live-8.29/` 确定性生成 15-B 协同层指标（终态 3/3、自主率 2/3、T6 人工等待 3m03s）与 R7 Skill 复用统计（live 调用 16 次、当晚沉淀 4 Skill）；无 evidence 目录时回退占位文本，复算语义不变。
- `scripts/trace_metrics.py` 无需改动，直接对 live trace 聚合生成 metrics.json。
- `EVIDENCE_HONESTY.md`：L3 行更新为实际 run 清单，L3/L4 证据入口改指 `evidence/live-8.29/`。
- 包卫生：剔除全部 `__pycache__` 字节码缓存（可再生成，非证据）。

## v1.4

相对 v1.3 为**纯增量**变更（replay 35/35 results 前缀仍 `e7314eb4`，与 v1.2/v1.3 逐位一致）。

### 网关（tools/notary_gateway.py）

- **advisory 建议模式（2b）**：custom intake 允许无测试上传（`advisory: true`）——流水线照常全流程（三道体检照跑），但 seal 时**不出公证书**（`should_issue_certificate` 谓词）；无测试且未声明 advisory 仍拒收
- fuzz 45 → **47**（+J7 advisory 建档/J8 公证书谓词）

### 前台（tools/notary_console.py）

- **聊天入口（办事大厅首页）**：单栏聊天面板——📎 ZIP 拖放（zipfile 解包分流源码/测试）+ 一句话问题 → `/api/assist` LLM 起草**确认卡**（**多轮补充对话**：history 客户端携带、服务端无会话态；LLM 仅交互层起草，人确认才提交）→ 现有 intake 通道；`/reset` 重新开始；单对话 localStorage 持久化
- **slash 命令**：输入框打 `/` 出菜单——`/skill 名字`（实时调网关 notary_skill.get 校验装入，注入 LLM 起草上下文——同一套 12 Skill 库服务客户与流水线两端）/example/skeleton/reset/help
- **LLM 接待（交互层）**：`CODENOTARY_LLM_KEY/BASE/MODEL` 环境变量注入（**key 绝不入包**）；未配置/失败自动降级为手动确认卡
- **PR 变更预览**：`/api/change/<sid>`（inhouse=author 修复逐文件 / external=送审补丁）；每个文件带**复制/下载**按钮；绿结果页=变更预览+公证书并排；advisory 终态=「修复建议（未公证）」专属展示+补救指引
- advisory 标志贯通（run_summary/list_runs/大厅卡片「建议已出」）

## v1.3

相对 v1.2 为**纯增量**变更：不修改任何既有场景行为（replay 35/35 results 前缀仍为 `e7314eb4`，与 v1.2 逐位一致）。

### 网关（tools/notary_gateway.py，0.8.0）

- **自带代码送修（custom-target intake）**：`notary_intake.submit_issue` 扩展接受 `source_files`/`test_files`/`repro_snippet`——客户自带靶场送修，inhouse 全流程照常
- **intake 严格检疫档**：自定义上传 reject-on-sight 扫描（16 类危险写法，译自 OpenClaw 威胁库候选集，先行通过 36 样本零误伤实测）+ 硬限额（≤10 文件/≤200KB/仅 .py/test_*.py）+ 补丁/自带互斥校验 + **无测试不受理**
- **沙箱执行**：自定义夹具的测试与复现统一加 rlimit 资源上限（内存 512MB/CPU 60s/句柄 64，`resource.setrlimit` 纯 stdlib）；curated 夹具调用路径逐位不变
- **复现接口**：`notary_flow.reproduce` 支持 payload 级 `repro_snippet` 覆盖（rca 现场提供复现代码；既有夹具行为不变）

### 前台（tools/notary_console.py）

- **办事大厅 `/desk`（客户视图，新增）**：面向送审开发者的三页旅程——大厅（双入口发起公证：我要修代码/我要审代码，多文件上传）/ 进度页（六步人话进度 + ESCALATED「需要你拿主意」裁决卡）/ 结果页（绿=公证书+浏览器验印+下载；红=人话版原因翻译+怎么办+重新送审预填）。客户视图零内部术语；与工程后台（四视图）同一批只读 API，网关零改动
- 工程后台头部加「办事大厅」入口链接

### 证据与开源（新增文件）

- `EVIDENCE_HONESTY.md`：全部证据的四层分层声明（L1 确定性评测无 LLM / L2 故障注入 / L3 真实平台有 LLM / L4 人工验收）+ 禁用表述表
- `tools/openclaw_threat_map.json` + `scripts/check_threat_map.py`：前期 OpenClaw 安全实证（arXiv:2603.10387）六攻击类×47 场景 ↔ 本系统防线的机器可核验映射（38 锚点，校验器篡改自检 fail-closed；独立工具，不在评测链内）
- README 新增「开源与许可声明」（Apache-2.0/零依赖/团队相关开源工作）

### 评测

- fuzz 41 → **45**（+J3–J6 自定义送修：合法建档/无测试拒收/危险代码检疫拒收/混合模式拒收）；全链连跑两次五产物逐位一致（results 前缀 `e7314eb4` 不变，fuzz 前缀 `c6d608db`）

## v1.2（复赛版，2026-08-28）

相对 v1.1 的变更统计（排除 `runs/` 瞬态产物与 `__pycache__`）：**新增 41 文件、修改 54 文件、删除 5 文件**（v1.1 的 sample_run/metrics.json 由 replay 后 `trace_metrics.py` 重新生成，改为不落盘于样例证据目录）。

### 网关（tools/notary_gateway.py，0.5 → 0.7.0）

- **Skill 运行时工具族**：`notary_skill.list/get/match`——12 触发信号决策表（`skills/match_table.json`）数据化，match 命中唯一 Skill；frontmatter `version`/`compat` 加载时强制校验（fail-closed），支持 `gateway>=X,<Y` 双边界子句
- **Skill 生命周期**：registry 追加式完整性台账 `skills/registry/index.json`（register/retire 条目含 content_sha256；篡改或未登记文件拒绝加载）；`supersedes` 版本链（match 解析最新未退役版本）；`notary_skill.retire` tombstone 回滚（退役最新版→前代自动恢复服务）
- **编排机制**：`notary_flow.request_rework`（REJECTED→AUTHORING 驳回重修环，预算 2、reason 必填、绿灯作废）；契约修订路径（ESCALATED→CONTRACTED，version/previous_hash 版本链）；ENTRY_STATES 入口状态白名单（契约前提交/跳级门禁/终态后改写均显式拒绝）；首版回滚语义修正（无备份=卸载，不留带病发布）；新一轮变异运行作废旧 rebuttal
- **权限审计**：ROLE_POLICY 角色白名单（23 个受限端点），越权显式拒绝 + `evidence/security_events.json` 落盘；trace 每行记录 role（缺失标 unknown）
- **断点恢复**：每次调用后 checkpoint 至 `runs/<sid>/checkpoint.json`，网关启动自动恢复（SIGKILL 实证：`scripts/eval_checkpoint.py`）
- **可观测**：`GET /metrics` Prometheus 文本导出（run 状态/按角色调用/安全事件/重修轮次）
- **工具接入参考实现**：`notary_intake.submit_issue`（CI webhook 形态，内容哈希幂等建档）

### 前台（tools/notary_console.py，新增）

只读六面板 Console（18091）：14 状态机图（当前高亮+回退边）/十棒时间线/三门禁 verdict 卡/trace 流/证据浏览器/metrics。

### 评测工具链（六 → 九组件）

- 新增 `scripts/eval_skill_coverage.py`（七审计：match 准确性/触发覆盖/孤儿/compat 卫生/台账完整性/世系 sanity/退役排除）
- 新增 `scripts/eval_checkpoint.py`（断点恢复实证）
- 新增 `scripts/capacity_probe.py`（容量实测 → evalset/capacity.json，运营数据）
- 新增 `scripts/alert_watchdog.py`（停滞/ESCALATED/REJECTED/安全事件四类告警）
- 新增 `scripts/one_click_setup.sh`（一键部署+自检+验收报告）
- `eval_replay.py`：角色化调用 + Skill 咨询 + 回退路径三驱动器；`eval_fuzz.py`：16→39 用例（+入口条件/环路防护/角色越权/Skill 安全/Skill 生命周期/intake）

### 评测集（v1.1.0 → v1.2.0，27 → 36 样本）

- **D1W 回退路径**（3）：accept_fix 黄灯重修 / 测试红驳回重修+发布回滚 / 契约演进
- **D5 真实仓库**（6）：pypa/packaging 三真实缺陷（#300/#673/#100）× 绿红双样本；真实源码逐字抽取（仅 import 扁平化，见 `tools/notary_target/VENDORED.md`）

### Skill 库

- 12 个 SKILL.md 补 `version`/`compat` frontmatter；新增 `skills/match_table.json`（决策表数据化）；新增 `skills/registry/index.json`（完整性台账）

### 兼容性声明

v1.2 对 v1.1 的全部既有场景保持行为一致：v1.1 的 26 个回放样本在 0.7.0 网关上的逐门禁判定与终态同 v1.1 逐位一致（eval_replay 回归即行为兼容声明的机器化证明）。trace 新增 role 字段、契约新增 version 字段为纯增量，不破坏 v1.1 证据包的可读性。

## v1.1（2026-08-25）

见附录 G（v1.0 → v1.1 变更表）。基线：`agentteams-v1.1-eval.tar.gz`，sha256 见随包 `.sha256` 文件。

## v1.0（2026-08-10）

全量基线：461 条目，五场景 dryrun 复验通过。
