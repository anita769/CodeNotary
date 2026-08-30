# EVIDENCE_HONESTY.md — 证据分层与诚实边界

> 本文件是 CodeNotary 全部提交证据的**分层声明**：每一份证据由哪一层机制产生、含不含 LLM、
> 发生在哪个环境，都在下表固化。任何材料（PPT / 视频 / 技术文档）中的表述以本表口径为准。
> 评审若在某份材料中发现与本表不一致的表述，以本表为准并欢迎质询。

## 一、证据四层分类

| 层 | 证据 | 含 LLM？ | 环境 | 性质 |
|---|---|---|---|---|
| **L1 确定性评测（裁决层）** | eval_replay 35/35（36 样本，1 live-only 豁免）、eval_fuzz 47/47、skill 覆盖审计七项、checkpoint 恢复实证、EVALUATION.md 全部汇总数字 | **无** | 任何干净机器 | 对网关裁决链的确定性重算；连跑逐位一致（results 前缀 `e7314eb4`，fuzz 前缀 `fe71d30b`） |
| **L2 故障注入实验** | T4：运行中途终止 Worker 进程 → wake 恢复 → 续跑至终态 | 无（平台层恢复机制） | 部署环境 | 韧性验证；针对进程级故障，不针对 LLM 输出 |
| **L3 真实平台运行（协同层）** | T1 green 主线（mb_delivery_semantics）、T2 red 主线（qb_external_leak）、T6 HITL 契约歧义（mb_delivery_semantics_ambiguous），及 2 个留存补充 run（qb_external_subtle 彩排、mb_external_breaking 补充演示） | **有**（Agent 判断层） | Cloud Studio（AgentTeams 官方平台） | 10 Agent + TeamLeader 真实协作；裁决仍由确定性网关作出 |
| **L4 人工验收** | 8.31 由真实用户在 Cloud Studio 独立按手册执行部署与录制；ECS 干净环境 T5 复现 | — | 两个互相独立的环境 | 真实用户验收 + 第二环境复现 |

## 二、表述口径（强制）

| 禁止表述 | 正确表述 |
|---|---|
| "系统成功率 35/35" | "裁决层确定性门禁回放 35/35 全过（36 样本，1 个 live-only 豁免）" |
| "评测证明多 Agent 协作可靠" | "协同层可靠性由 L3 真实运行证明；L1 评测证明的是裁决层的判定正确性与可复算性" |
| "fuzz 证明系统不会崩" | "fuzz 47/47 证明网关对畸形输入全 fail-closed（显式拒绝/报错，零静默放行）" |
| 任何把 L1 批量数字说成发生在真实平台的措辞 | L1 数字一律标注"确定性回放（无 LLM）" |

## 三、为什么这么分层

评审判定能力（裁决层）的基准要求**标注正确、可逐位复算**——所以用确定性回放，量可以大；
评判协作能力（协同层）的基准要求**真实平台、真实模型、不可批量伪造**——所以用真实运行，量不在大而在链路完整。
知道哪一层该用什么方式评，是本系统评测设计的一部分（增刊第 13 章三公理）。

## 四、可复算入口

- L1 全部数字：`附录 F` 命令清单（先 `sha256sum -c SHA256SUMS.txt` 验证包完整性，再跑六脚本链 dryrun→replay→coverage→fuzz→checkpoint→report）
- L3 证据：`evidence/live-8.29/` 独立目录（5 个真实 run 的 trace.jsonl / verdicts / manifest.json / certificate.md + pace 日志 + Skill registry + AgentTeams 平台日志，采集来源与视频逐段对账见该目录 PROVENANCE.md），与包根 `runs/`（L1 回放产物）物理隔离
- L4 证据：录制日操作手册执行记录与第二环境（ECS）复现记录
- 本文件自身随包封印，纳入 SHA256SUMS.txt
