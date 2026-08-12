# AgentTeams 变更公证任务消息

10 个业务 Worker、独立 TeamLeader Worker `codenotary-leader` 以及 `codenotary` Team 创建完成后，在 Element Web/Matrix 会话列表中找到名称以 `Team` 开头、对应 `codenotary` 的 Team 房间。

进入 Team 房间后，在输入框先输入并选中 `@<team_leader_name>`，再把下面的公证任务复制到这条 @ 消息里发送。不要把任务发给 `manager`。`manager` 用于创建和管理 Agent/Team；Team 房间中的 leader 用于接收业务任务并调度 Worker。

请逐个任务发送：等上一个任务的公证报告完整输出后，再发送下一个。不要同时发送多个任务，避免 Team 并发调度时上下文和工具状态互相干扰。

每条消息只包含用户能自然提供的信息（issue 来源与 scenario_id）。源码、复现、门禁执行、证据收集等信息应由 Agent 通过公证工具网关主动获取。

## 任务一（主演示）：交付语义缺陷自研修复（green 路径）

```text
@<team_leader_name>

请让你的 Team 公证一条新的变更任务。

scenario_id: mb_delivery_semantics
模式：流水线自研修复（inhouse）
issue 来源：生产事故复盘 ISSUE-301

业务背景：
复盘一起诡异的生产事故：消费者短暂抖动期间，多条订单消息未被处理，
却没有告警、没有残留记录，像凭空消失。排查怀疑消息分发器的交付语义
有问题——这个缺陷语法上看不出任何毛病，需要完整诊断和验证。

请按完整公证流程处理：检疫、分诊、根因诊断、契约冻结、盲测对抗、
确定性门禁、终审、发布与复盘，并输出本次公证报告。
```

## 任务二：外部 AI 变更送审 · 隐蔽夹带（subtle red 路径）

任务一报告输出后发送。**看点：功能完全正确、测试全绿，仍被公证处拒绝。**

```text
@<team_leader_name>

请让你的 Team 公证一条新的变更任务。

scenario_id: qb_external_subtle
模式：外部 AI 变更送审（external）
issue 来源：外部 AI 编程助手直接提交的修复变更（ISSUE-101）

业务背景：
针对 Mailbox 空队列弹出的缺陷，一个外部 AI 编程助手提交了修复补丁。
功能测试显示它确实修好了问题。但它未经任何评审——AI 生成的代码不
能直接合入主干。补丁已随送审提交到网关，请对它做完整的检疫、盲测
与门禁检验，给出公证结论。
```

## 任务三：外部 AI 变更送审 · 粗糙补丁（red 路径，备选）

```text
@<team_leader_name>

请让你的 Team 公证一条新的变更任务。

scenario_id: qb_external_sloppy
模式：外部 AI 变更送审（external）
issue 来源：外部 AI 编程助手直接提交的修复变更（ISSUE-101）

业务背景：
另一个外部 AI 助手也提交了针对同一缺陷的补丁，质量未知。补丁已在
网关内，请完整检验并给出公证结论。
```

## 任务四：复合缺陷修复（green 路径，备选）

场景 `mb_router_compound`：容量 off-by-one + drain 耗尽 panic 双缺陷交织。

## 任务五：简单边界修复（green 路径，备选/快速演示）

场景 `qb_inhouse_fix`：单文件单缺陷（Mailbox 空弹出守卫恒真），流程相同但规模最小，适合 3 分钟快速演示。

## 预期结果速查

| 场景 | 预期最终状态 | 关键看点 |
| --- | --- | --- |
| `mb_delivery_semantics` | NOTARIZED → RELEASED | 语义级缺陷（先取后交付=失败即丢消息；未知 channel 静默丢弃）；契约含重试上限/死信审计/严格 FIFO 保序；盲测 7 用例；变异 10 个体 3 个等价幸存逐条举证复算 1.00；发布冒烟 11 用例 |
| `qb_external_subtle` | REJECTED | **测试门禁全绿**、变异门禁 green（含举证），但惯例门禁 red：补丁夹带未审计的消息落盘（潜在数据外泄）——测试证明功能，公证审查行为边界 |
| `qb_external_sloppy` | REJECTED | 检疫 3 项 high（硬编码凭据/os.system）；盲测红灯（空 pop 返回 None 违背契约）；惯例 5 条 veto |
| `mb_router_compound` | NOTARIZED → RELEASED | 复合边界缺陷；6 变异体 2 等价幸存双举证 |
| `qb_inhouse_fix` | NOTARIZED → RELEASED | 最小完整链路：单字符修复也走完全部 10 棒 |

如果 Team 要求你人工提供源码、测试或门禁结果，可以提醒：

```text
请通过已配置的 HTTP 公证工具网关主动获取，不要让我人工收集证据。
```
