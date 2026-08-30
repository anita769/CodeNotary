# CodeNotary 公证书

| 项 | 值 |
|---|---|
| 运行 | `mb_delivery_semantics` |
| Issue | ISSUE-301 — 消费者瞬时故障期间消息凭空消失，且无任何人发现 |
| 终态 | **RELEASED** |
| 契约 | frozen_hash `86f4a9b798610c53…`（版本 v1） |
| 重修轮次 | 0/2 |
| 安全事件 | 0 次越权拒绝 |
| 人工介入 | 0 次升级 |

## 门禁裁决

| 门禁 | 判定 | 摘要 |
|---|---|---|
| convention | green | 1 findings (0 veto-class) |
| mutation | green | mutation score after rebuttal 1.00 (7 killed, 0 survived, 3 exempted) |
| test_pass | green | baseline+blind tests: 11 run, all passed |

## 证据

本公证书所涉全部判定由确定性代码执行（LLM 仅参与判断环节，不参与裁决）。
证据包封印清单见同目录 `manifest.json`（逐文件 SHA-256）；
逐调用审计见 `trace.jsonl`（含角色与载荷/结果哈希链）。
第三方复算方式见代码包 EVALUATION.md 与技术文档附录 F。
