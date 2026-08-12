# CodeNotary × AgentTeams 可执行代码包

CodeNotary（代码公证处）是面向 AI 生成代码的可信交付流水线：10 个最小权限 Agent 组成质量门闭环，LLM 只做判断、确定性代码只做裁决，全程证据可封存、可回放。

本目录是 CodeNotary 的 **AgentTeams 可执行代码包**（GOAI 赛道一复赛提交物），包含运行入口、依赖说明、配置文件、样例输入输出和运行证据。

## Demo 视频

本仓库包含 v3 剪辑版 demo 视频：

- 视频：`demo/goai-agentteams-demo-v3.mp4`

视频展示 AgentTeams 多智能体协作流程：创建 10 个 worker、Team 任务分派、triage / contract / tester / release / postmortem 等角色协作，以及最终状态确认。该版本保留关键消息节点，并删除明显停顿段。

## 包结构

```
agentteams/
├── README.md                      # 本文件
├── at/                            # AgentTeams 运行配置
│   ├── AGENTTEAMS_RUNBOOK.md      #   部署运行手册（从网关启动到判通标准）
│   ├── create_agents_messages.md  #   10 Worker + Team 的一段式创建消息
│   ├── run_demo_task_message.md   #   两个公证任务消息（green / red 路径）
│   ├── team_spec.json             #   Team 拓扑、工作流、风险策略（机器可读）
│   ├── AgentTeam.md               #   Team 形态与核心不变式说明
│   └── agentteams.env.example     #   配置清单样例（不含任何真实密钥）
├── agents/<role>/Agent.md         # 10 个角色的完整身份规约（评审追溯用）
├── skills/                        # 8 个种子 Skill（SKILL.md）+ registry/ 沉淀产出
├── tools/
│   ├── notary_gateway.py          # 公证工具网关：确定性核心，纯标准库
│   ├── notary_target/             # 演示目标服务（含预置边界缺陷）
│   └── tool_catalog.json          # 工具清单与 MCP 迁移映射
├── scenarios/                     # 样例输入
│   ├── qb_inhouse_fix.json        #   green 路径：流水线自研修复
│   └── qb_external_sloppy.json    #   red 路径：外部 AI 变更送审
├── scripts/
│   └── local_dryrun.py            # 无 LLM 全流程自检（真实执行所有门禁）
└── evidence/sample_run/           # 样例运行证据（local_dryrun 的真实产出）
```

## 依赖

- Python 3（网关与自检脚本仅用标准库，无第三方包）。
- Docker + AgentTeams（运行多 Agent 协作；安装与配置见 `at/AGENTTEAMS_RUNBOOK.md`）。
- 本代码包不包含、也不需要任何模型 API Key——LLM 凭证由 AgentTeams 安装器持有。

## 运行入口

两条路径，按需选择：

**A. 无 LLM 自检（约 1 分钟，验证确定性核心）**

```bash
python3 scripts/local_dryrun.py
```

现场执行：检疫扫描 → 契约 sha256 冻结 → 盲测 unittest → 变异门禁（含等价变异体反驳复算）→ 惯例门禁 → 状态机推进到 NOTARIZED/REJECTED → 发布冒烟 → 证据封印。产物写入 `runs/`，并收集到 `evidence/sample_run/`。

**B. AgentTeams 全流程（真实 LLM 多 Agent 协作）**

```bash
python3 tools/notary_gateway.py --host 0.0.0.0 --port 18090
```

然后按 `at/AGENTTEAMS_RUNBOOK.md` 完成 Worker/Team 创建与任务发送。

## 样例输入输出

- 输入：`scenarios/qb_inhouse_fix.json`（issue 报告）、`scenarios/qb_external_sloppy.json`（issue + 外部送审变更）。
- 输出：`evidence/sample_run/<scenario_id>/` 下的真实运行产物——`contract.json`（含 frozen_hash）、`verdicts/*.json`（test_pass / mutation / convention）、`survivors.md`、`rebuttals.json`、`trace.jsonl`（网关全调用轨迹）、`manifest.json`（sha256 封印清单）等。

## 核心不变式

LLM 输出永不驱动状态转移。所有门禁分数、red/yellow/green 裁决与流水线状态转移由 `tools/notary_gateway.py` 中的确定性代码完成（移植自主仓库 `codenotary/state_machine.py`，14 状态、非法转移抛 `IllegalTransition`）。author/tester 盲测隔离在工具契约层强制执行：网关中不存在能向对方暴露产物的工具。
