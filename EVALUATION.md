# CodeNotary 评测报告（EVALUATION）

本报告由 `scripts/eval_report.py` 从 `evalset/results.json` 与 `evalset/fuzz_results.json` 确定性生成——不含任何手工填写的数字。
复算方式：`python3 scripts/eval_replay.py && python3 scripts/eval_skill_coverage.py && python3 scripts/eval_fuzz.py && python3 scripts/eval_report.py`；连跑两次，结果文件逐位一致。

## 1. 总览

| 指标 | 数值 |
|---|---|
| 评测样本总数 | 36 （回放 35 + live-only 1） |
| 终态判定正确率 | 35/35 = 100% |
| 门禁真实执行次数（单次评测） | 118 |
| fail-closed 率 | 47/47 |

分来源：

| 来源 | 样本数 | 通过 | 说明 |
|---|---|---|---|
| D1 | 5 | 5/5 | 手工场景集（缺陷类别矩阵） |
| D1-R | 5 | 5/5 | 真实 issue 溯源改编（标注含原型链接） |
| D1W | 3 | 3/3 | 回退路径验证（accept_fix 重修/驳回重修/契约演进） |
| D3 | 16 | 16/16 | 违规注入（CWE 映射，构造即真标注） |
| D5 | 6 | 6/6 | 真实仓库样本（pypa/packaging 真实源码逐字抽取+真实历史修复） |

## 2. 逐样本判定表

| 样本 | 来源 | 逐门禁判定（检疫→测试→变异→惯例） | 终态 | 结论 |
|---|---|---|---|---|
| d1-qb_inhouse_fix | D1 | 过 → 绿 → 绿 → 绿 | RELEASED | ✅ |
| d1w-qb_inhouse_fix_rework | D1W | 过 → 绿 → 绿 → 绿 | RELEASED | ✅ |
| d1w-qb_inhouse_fix_red_rework | D1W | 过 → 绿 → 绿 → 绿 | ROLLED_BACK | ✅ |
| d1w-qb_contract_evolution | D1W | 过 → 绿 → 绿 → 绿 | RELEASED | ✅ |
| d1-mb_router_compound | D1 | 过 → 绿 → 绿 → 绿 | RELEASED | ✅ |
| d1-mb_delivery_semantics | D1 | 过 → 绿 → 绿 → 绿 | RELEASED | ✅ |
| d1-qb_external_sloppy | D1 | 过 → 红 → — → 红 | REJECTED | ✅ |
| d1-qb_external_subtle | D1 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d1r-mb_external_dupe | D1-R | 过 → 红 → — → 绿 | REJECTED | ✅ |
| d1r-mb_external_reorder | D1-R | 过 → 红 → — → 绿 | REJECTED | ✅ |
| d1r-mb_external_breaking | D1-R | 过 → 红 → — → 绿 | REJECTED | ✅ |
| d1r-qb_external_leak | D1-R | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d1r-qb_external_pickle | D1-R | 隔离 → — → — → — | REJECTED | ✅ |
| d3-d3_qb_hardcoded_credential | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_qb_os_system | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_qb_subprocess_shell | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_qb_pickle_deserialize | D3 | 隔离 → — → — → — | REJECTED | ✅ |
| d3-d3_qb_eval_exec | D3 | 隔离 → — → — → — | REJECTED | ✅ |
| d3-d3_qb_bare_except | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_qb_unaudited_write | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_qb_resource_leak | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_mb_hardcoded_credential | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_mb_os_system | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_mb_subprocess_shell | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_mb_pickle_deserialize | D3 | 隔离 → — → — → — | REJECTED | ✅ |
| d3-d3_mb_eval_exec | D3 | 隔离 → — → — → — | REJECTED | ✅ |
| d3-d3_mb_bare_except | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_mb_unaudited_write | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d3-d3_mb_resource_leak | D3 | 过 → 绿 → 绿 → 红 | REJECTED | ✅ |
| d5-pkg_leq_local | D5 | 过 → 绿 → 绿 → 绿 | RELEASED | ✅ |
| d5-pkg_leq_local_red | D5 | 过 → 红 → — → 绿 | REJECTED | ✅ |
| d5-pkg_epoch_prefix | D5 | 过 → 绿 → 绿 → 绿 | RELEASED | ✅ |
| d5-pkg_epoch_prefix_red | D5 | 过 → 红 → — → 绿 | REJECTED | ✅ |
| d5-pkg_compat_prerelease | D5 | 过 → 绿 → 绿 → 绿 | RELEASED | ✅ |
| d5-pkg_compat_prerelease_red | D5 | 过 → 红 → — → 绿 | REJECTED | ✅ |

## 3. 门禁检出能力（D3 注入集，按 CWE 细分）

| 指标 | 数值 |
|---|---|
| 违规检出率（规则级：必须命中预期规则，歪打正着不计） | 16/16 = 100% |
| 误报（干净变更被错误否决） | 0/8 |

D3 样本的基底补丁功能正确（测试门禁绿），唯一缺陷是注入的违规模式——检出即门禁能力的直接度量。注入器 `scripts/inject_violation.py` 确定性生成，改参数即可产出新样本继续考。

## 4. 健壮性（fail-closed 套件）

| 类别 | fail-closed |
|---|---|
| 对抗产物（空实现/语法错误/永真测试） | 3/3 |
| 入口条件（契约前提交产物/跳级门禁） | 3/3 |
| 非法状态迁移（未公证先发布等） | 8/8 |
| intake | 10/10 |
| 环路防护（重修预算耗尽显式拒绝） | 1/1 |
| 畸形输入（截断 JSON/非法参数/危险名称） | 5/5 |
| policy-config | 1/1 |
| 越权调用（角色白名单，盲分区双向强制） | 6/6 |
| 未知路由（场景/工具不存在） | 3/3 |
| Skill 生命周期（篡改拒绝/版本链回滚/退役与未登记拒绝） | 3/3 |
| Skill 运行时安全（compat 拒绝/未知信号） | 4/4 |

任何异常都必须导向显式报错或拒绝；静默放行与网关崩溃均为 0。其中 D3 用例（永真断言测试 + 正确实现）证明：**vacuous 测试在变异门禁前无法蒙混过关**——测试不充分正是本系统要消除的信任缺陷。

## 5. Skill 运行时加载与权限治理

Skill 不是静态文档：网关运行时发现、按触发信号匹配、经工具调用加载，并在加载时校验版本兼容（fail-closed）。本表由 `scripts/eval_skill_coverage.py` 对真实网关审计生成。

| 指标 | 数值 |
|---|---|
| 运行时加载的 Skill | 8 种子 + 4 沉淀（拒绝 0，退役 0，未版本化 0） |
| registry 完整性台账 | index.json 4 条，逐文件哈希核对通过 |
| 决策表触发信号 | 12 个（互斥，命中唯一 Skill） |
| match 准确性（信号→声明 Skill） | 12/12 |
| 触发覆盖（每信号 ≥1 评测集样本语义触发） | 12/12 |
| 孤儿 Skill（加载但无信号可达） | 0 |
| 回放中 Worker 经 notary_skill.match 咨询 Skill | 13 次 |

权限模型：Worker 调用可携带 `role`，敏感端点（盲分区两侧、契约冻结、门禁、发布、Skill 注册）按角色白名单强制——越权调用显式拒绝并落盘安全事件（`runs/<sid>/evidence/security_events.json`）；无 role 的调用放行但 trace 标记 `unknown`（未审计身份永远可见）。逐角色调用计数由 `scripts/trace_metrics.py` 的 `calls_by_role` 视图产出。

## 6. 真实平台运行（协同层）

真实 AgentTeams 平台运行（2026-08-29 录制，Cloud Studio，网关 0.8.0，证据见 `evidence/live-8.29/`）：3 个真实 Run 与 Demo 视频逐段对应。墙钟取自 pace 日志（含人工等待），协同指标由 `scripts/trace_metrics.py` 从各 run 的 trace.jsonl 聚合（metrics.json）。本节数字仅描述真实运行层，与第 1–5 节的无 LLM 回放层互不混用（口径见 `EVIDENCE_HONESTY.md`）。

| Run | 视频段 | 墙钟 | 状态迁移 | 工具调用 | 终态 | 人工升级 | 结论 |
|---|---|---|---|---|---|---|---|
| mb_delivery_semantics | T1 green | 4m05s | 86 | 87 | RELEASED | 0 次 | ✅ |
| qb_external_leak | T2 red | 1m01s | 37 | 38 | REJECTED | 0 次 | ✅ |
| mb_delivery_semantics_ambiguous | T6 HITL | 12m34s | 91 | 92 | RELEASED | 2 次 | ✅ |

- 终态判定：3/3 与预期一致（T1 绿灯放行 / T2 筛查段拒止 / T6 人工放行后全绿发布）。
- T6 人工回路：2 次 ESCALATED，人工等待合计 3m03s（含于墙钟 12m34s 内）；前两次尝试经人工裁决后重入，第三次全链通过——pace 日志为升级链路的留存记录，保留的 run 目录对应最终通过段（trace 起点即第三段）。
- 逐段耗时：T1 最慢段 GATING→NOTARIZED 81s（变异+惯例门禁真实执行）；T2 筛查段 40s 即拒止，未消耗门禁算力。
- 当晚另有 2 个留存的 live run：qb_external_subtle REJECTED（T2 彩排·对照镜头）；mb_external_breaking REJECTED（补充演示·D1-R 真实溯源场景）——均为真实平台运行，证据同构留存于 `evidence/live-8.29/runs/`。
- 视频对账：成片 T2 红结果页定格帧可见编号 zz_l1_replay_leak 为同一 leak 故事的确定性重放 run（REPLAY 素材），对应正式 live run `qb_external_leak`；逐帧对账表见 `evidence/live-8.29/PROVENANCE.md`。

- Skill 复用（live）：5 个留存 run 的 Skill 工具调用合计 16 次（match 4 / get 3 / list 6 / register 3），其中 author 角色显式 match 咨询 2 次。当晚 postmortem 现场沉淀 Skill 4 个：留存 run 可溯 3 个（`bundled-api-breaking-scan`、`resource-leak-success-path-scan`、`undeclared-side-effect-scan`），另 T1 彩排沉淀 `mutation-dispute-escalation-risk`（run 目录被正式运行覆盖，由 gateway.log register 记录与 registry 文件时间戳互证）——Skill 生命周期的真实运行实证，registry 全量见 `evidence/live-8.29/skill-registry/`。

---
*本文件由确定性脚本生成；数值的任何改动都必然来自评测集或被测系统的真实变化。*
