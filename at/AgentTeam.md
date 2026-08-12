# CodeNotary AgentTeam

这个文件描述 CodeNotary（代码公证处）使用的 Team 形态。主运行路径是 AgentTeams + 真实 LLM Worker + HTTP 公证工具网关（确定性核心）。

## AgentTeams 运行时

| AgentTeams 概念 | CodeNotary 设计 |
| --- | --- |
| Manager 房间 | 接收自包含的 Agent 创建消息 |
| Team 房间 | Matrix 会话列表中名称以 `Team` 开头；用户通过 `@<team_leader_name>` 发送变更公证任务 |
| TeamLeader Worker | 创建 Team 时由 manager 生成的独立 Worker `codenotary-leader` |
| Worker 房间 | 运行 10 个角色明确的业务 LLM Agent（sentinel / triage / rca / contract / author / tester / convention / gatekeeper / release / postmortem） |
| Worker 运行时 | 统一使用 `qwenpow`（`copow`/`QwenPaw`） |
| 创建策略 | `manager` 串行创建 10 个业务 Worker；创建 Team 时再生成独立 TeamLeader Worker `codenotary-leader`；禁止把业务 Worker 指定为 leader |
| AgentSpec | 10 个业务 Worker 内联在 `at/create_agents_messages.md` |
| 任务输入 | `at/run_demo_task_message.md` 中的变更公证任务 |
| 工具调用 | HTTP 公证工具网关（`tools/notary_gateway.py`，纯标准库） |
| Skill Registry | 当前运行时使用创建消息中的内联 Skill 语义；`skills/*/SKILL.md` 与 `skills/registry/` 用于评审和后续替换 |

## 核心不变式

**LLM 输出永不驱动状态转移。** Worker 只产出文本、关键字与结构化声明；检疫扫描、契约冻结（sha256）、测试门禁（真实 unittest 执行）、变异门禁（现场变异 + 盲测计分）、惯例检查、流水线状态机、发布门禁、证据封印全部由网关的确定性代码完成。这与 CodeNotary 主仓库 `codenotary/state_machine.py` 的硬约束一致。

## 盲测隔离

author 与 tester 的隔离在工具契约层强制执行：网关中不存在任何能把 tester 产物暴露给 author、或把 author 实现暴露给 tester 的工具。author 上下文 = 契约 + 源码 + 诊断；tester 上下文 = 契约 + 基线公开测试。

## 工作流

1. TeamLeader `codenotary-leader` 接收 Team 房间中的公证任务，提取 `scenario_id`，调度业务 Worker。
2. `sentinel` 检疫扫描（注入模式 / 硬编码凭据 / 文件指纹），critical 发现由网关确定性隔离。
3. `triage` 判定 accept / reject / escalate，关键字由网关确定性解析并转移状态。
4. `rca` 只读复现与诊断，产出结构化 root_cause + evidence。
5. `contract` 冻结验收契约（sha256），声明盲测分区。
6. `author` 按契约实现（inhouse）或待命答辩（external）；`tester` 只凭契约写对抗性盲测。
7. TeamLeader 直接调用确定性门禁：测试门禁 → 变异门禁（幸存者经 author 反驳后复算）→ `convention` 解读惯例检查。
8. `gatekeeper` 只读全部 verdict，摘要异常与冲突；yellow 升级人工。
9. `release` 仅在 NOTARIZED 时发布（网关强制），冒烟失败可回滚。
10. `postmortem` 沉淀增量 Skill 并 sha256 封印证据包；TeamLeader 汇总公证报告。

## Demo 场景

| 场景 | 模式 | 预期路径 |
| --- | --- | --- |
| `qb_inhouse_fix` | 流水线自研修复 | green：契约冻结 → 盲测 6 用例全绿 → 变异幸存者 M03 经举证豁免 → NOTARIZED → RELEASED → 证据封印 |
| `qb_external_sloppy` | 外部 AI 变更送审 | red：检疫命中凭据/os.system → 盲测红灯（契约违背）→ 惯例门禁 5 条 veto → REJECTED → 证据封印 |
