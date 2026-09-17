# CodeNotary × AgentTeams 可执行代码包

CodeNotary（代码公证处）是面向 AI 生成代码的可信交付流水线：10 个最小权限 Agent 组成质量门闭环，LLM 只做判断、确定性代码只做裁决，全程证据可封存、可回放。

本目录是 CodeNotary 的 **AgentTeams 可执行代码包**（GOAI 赛道一复赛提交物），包含运行入口、依赖说明、配置文件、样例输入输出和运行证据。

## 包结构

```
agentteams/
├── README.md                      # 本文件
├── EVALUATION.md                  # 评测报告（脚本生成，可复算；§6 为真实平台运行指标）
├── EVIDENCE_HONESTY.md            # 证据分层与诚实边界（引用任何数字前请先读）
├── CHANGES.md                     # 版本变更记录（v1.0 → v1.5）
├── LICENSE                        # Apache-2.0
├── SHA256SUMS.txt                 # 全包文件封印清单（包根执行 sha256sum -c 校验）
├── notary.json                    # 公证策略配置（门禁阈值/风险策略，机器可读）
├── at/                            # AgentTeams 运行配置
│   ├── AGENTTEAMS_RUNBOOK.md      #   部署运行手册（从网关启动到判通标准）
│   ├── create_agents_messages.md  #   10 Worker + Team 的一段式创建消息
│   ├── run_demo_task_message.md   #   两个公证任务消息（green / red 路径）
│   ├── team_spec.json             #   Team 拓扑、工作流、风险策略（机器可读）
│   ├── AgentTeam.md               #   Team 形态与核心不变式说明
│   └── agentteams.env.example     #   配置清单样例（不含任何真实密钥）
├── agents/<role>/Agent.md         # 10 个角色的完整身份规约（评审追溯用）
├── skills/                        # 8 个种子 Skill（SKILL.md）+ 决策表 + registry/ 沉淀产出
├── tools/
│   ├── notary_gateway.py          # 公证工具网关：确定性核心，纯标准库（2169 行，34 端点）
│   ├── notary_console.py          # 前台双页面服务：Console 四视图 + 办事大厅 /desk
│   ├── notary_target/             # 演示目标服务（含预置边界缺陷 + D5 真实仓库 vendored 源码）
│   ├── tool_catalog.json          # 工具清单与 MCP 迁移映射
│   └── openclaw_threat_map.json   # 威胁映射（38 锚点，scripts/check_threat_map.py 校验）
├── scenarios/                     # 样例输入
│   ├── qb_inhouse_fix.json        #   green 路径：流水线自研修复
│   ├── qb_external_sloppy.json    #   red 路径：外部 AI 变更送审
│   ├── 结算缺陷演示包.zip           #   办事大厅演示可直接上传
│   └── …                          #   全部评测场景 JSON（D1/D1-R/D1W/D3/D5 各源）
├── evalset/                       # 评测数据集：manifest v1.2.0（36 样本 + 期望标注，公开可审阅）
│                                  #   + results/fuzz_results/skill_coverage/checkpoint_results（机器结果）
├── scripts/                       # 评测工具链（六脚本链）+ 部署与运维
│   ├── local_dryrun.py            #   无 LLM 全流程自检（真实执行所有门禁）
│   ├── eval_replay.py             #   36 样本回放：驱动真实网关 + 逐项比对期望
│   ├── eval_skill_coverage.py     #   Skill 运行时七审计
│   ├── eval_fuzz.py               #   47 用例 fail-closed 套件
│   ├── eval_checkpoint.py         #   断点恢复实证（SIGKILL → 续跑 → 终态）
│   ├── eval_report.py             #   → EVALUATION.md（确定性生成，含 §6 live 指标）
│   ├── trace_metrics.py           #   trace 协同指标聚合（回放/真实运行通用）
│   └── one_click_setup.sh         #   一键起网关+前台+自检+验收报告（评委入口）
├── runs/                          # 36 个回放样本的证据目录（无 LLM 回放产物，可复算）
├── demo/
│   ├── demo_new.mp4          	   # Demo 视频
│   ├── demo-分镜介绍.md            # 视频分镜内容介绍
└── evidence/
    ├── sample_run/                # 样例运行证据（local_dryrun 的真实产出）
    └── live-8.29/                 # 真实平台运行证据链路（2026-08-29 示例)
```

## 依赖

- Python 3（网关与自检脚本仅用标准库，无第三方包）。
- Docker + AgentTeams（运行多 Agent 协作；安装与配置见 `at/AGENTTEAMS_RUNBOOK.md`）。
- 本代码包不包含、也不需要任何模型 API Key——LLM 凭证由 AgentTeams 安装器持有。

## 快速上手

**A. 一分钟自检（无 LLM，验证裁决层——评委复算入口）**

```bash
python3 scripts/local_dryrun.py
```

现场执行：检疫扫描 → 契约 sha256 冻结 → 盲测 unittest → 变异门禁（含等价变异体反驳复算）→ 惯例门禁 → 状态机推进到 NOTARIZED/REJECTED → 发布冒烟 → 证据封印。产物写入 `runs/`，并收集到 `evidence/sample_run/`。

复算全部发布数字（六脚本链，顺序固定）：

```bash
python3 scripts/local_dryrun.py && python3 scripts/eval_replay.py \
  && python3 scripts/eval_skill_coverage.py && python3 scripts/eval_fuzz.py \
  && python3 scripts/eval_checkpoint.py && python3 scripts/eval_report.py
```

连跑两次，`evalset/results.json` / `EVALUATION.md` 逐位一致即为确定性复现成立（附录 F 有完整命令清单与发布值哈希前缀）。


**B. AgentTeams 全流程（真实 LLM 多 Agent 协作）**

```bash
python3 tools/notary_gateway.py --host 0.0.0.0 --port 18090
python3 tools/notary_console.py --port 18091
```

然后按 `at/AGENTTEAMS_RUNBOOK.md` 完成 Worker/Team 创建与任务发送；办事大厅上传的受理单此时会被团队自动接单推进至终态，公证书/退件原因回显前台。2026-08-29 的真实运行证据（含 pace 日志与平台容器日志）见 `evidence/live-8.29/`。

## 样例输入输出

- 输入：`scenarios/qb_inhouse_fix.json`（issue 报告）、`scenarios/qb_external_sloppy.json`（issue + 外部送审变更）、`scenarios/结算缺陷演示包.zip`（办事大厅上传演示：埋了边界缺陷的微型订单系统，buggy 版挂 4 个测试、修复版全绿）。
- 输出：`evidence/sample_run/<scenario_id>/` 下的真实运行产物——`contract.json`（含 frozen_hash）、`verdicts/*.json`（test_pass / mutation / convention）、`survivors.md`、`rebuttals.json`、`trace.jsonl`（网关全调用轨迹）、`manifest.json`（sha256 封印清单）等。

## 核心不变式

LLM 输出永不驱动状态转移。所有门禁分数、red/yellow/green 裁决与流水线状态转移由 `tools/notary_gateway.py` 中的确定性代码完成（移植自主仓库 `codenotary/state_machine.py`，14 状态、非法转移抛 `IllegalTransition`）。author/tester 盲测隔离在工具契约层强制执行：网关中不存在能向对方暴露产物的工具。

## 证据复算（双层复现）

证据包不采信自述，支持第三方零 LLM 复算：

```bash
make verify EVIDENCE=evidence-pack.zip
```

复算四层：Ed25519 签名（封印者身份，公钥在 `keys/notary_ed25519.pub`）→ manifest 哈希链 + trace 封印前缀 → 契约 frozen_hash 重算 → 三门禁在包内代码上重跑并与封存 verdict 比对。第一层（复算）任何人可跑；第二层（含 LLM 的完整重跑）需自带模型 key。

## 开源与许可声明

- **许可证**：Apache-2.0（见包根 `LICENSE`）；第三方依赖为零（纯 Python 标准库），vendored 真实源码样本（pypa/packaging）的出处与许可见 `tools/notary_target/VENDORED.md`。
- **发行状态**：本包为 GOAI 复赛提交件（v1.5 终版），随赛交付；
- **证据口径**：包内全部评测与运行证据的分层声明见 `EVIDENCE_HONESTY.md`——哪些是无 LLM 的确定性回放、哪些是真实平台运行，逐类固化，引用数字前请先读它。
- **复现入口**：`scripts/one_click_setup.sh` 一键起全套；评审复算命令清单见技术文档增刊附录 F。
