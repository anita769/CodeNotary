# CodeNotary — 常用入口

.PHONY: verify

# 零 LLM 复算单个证据包：签名 → 哈希链 → 契约 → 门禁重跑比对
# 用法: make verify EVIDENCE=evidence-pack-pr1.zip
verify:
	python3 scripts/verify_evidence.py $(EVIDENCE)
