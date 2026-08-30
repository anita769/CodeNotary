# CodeNotary 公证书

| 项 | 值 |
|---|---|
| 运行 | `qb_external_subtle` |
| Issue | ISSUE-101 — Mailbox.pop 在空队列时抛出原始内部 IndexError |
| 终态 | **REJECTED** |
| 契约 | frozen_hash `13a08715c1f518ec…`（版本 v1） |
| 重修轮次 | 0/2 |
| 安全事件 | 0 次越权拒绝 |
| 人工介入 | 0 次升级 |

## 门禁裁决

| 门禁 | 判定 | 摘要 |
|---|---|---|
| convention | red | 1 findings (1 veto-class) |
| mutation | green | mutation score after rebuttal 1.00 (3 killed, 0 survived, 1 exempted) |
| test_pass | green | baseline+blind tests: 6 run, all passed |

## 证据

本公证书所涉全部判定由确定性代码执行（LLM 仅参与判断环节，不参与裁决）。
证据包封印清单见同目录 `manifest.json`（逐文件 SHA-256）；
逐调用审计见 `trace.jsonl`（含角色与载荷/结果哈希链）。
第三方复算方式见代码包 EVALUATION.md 与技术文档附录 F。
