---
name: resource-leak-success-path-scan
description: distilled from run qb_external_leak
version: 1.0.0
compat: gateway>=0.8
---

name: resource-leak-success-path-scan
description: Use when reviewing an external submission that bundles extra functionality (e.g. session history preload) — check resource lifecycle: any opened handle (file, socket, lock) must be closed on ALL paths (with/finally), including the SUCCESS path; a handle opened but never closed/never used is a CWE-772 resource leak invisible to functional tests. Bundled features with leaks are a reject criterion even when the core fix is correct and tests pass.

# Resource-Leak Success-Path Scan (成功路径资源泄漏扫描)

## When to use
- Reviewing an external submission where the core fix is correct but extra functionality is bundled (e.g. "preload session history", "audit logging") — verify the resource lifecycle of the bundled code.
- The issue explicitly warns about resource release (EB-6: resources must be released on ALL paths; success-path leak is a defect, CWE-772).

## Steps
1. Inventory every resource acquisition in the diff: open()/FileIO, socket, lock, tempfile, subprocess pipes, mmap — including inside __init__ / constructors and helper methods.
2. For each acquisition, verify release on ALL paths: with-statement, try/finally, or explicit close() on success AND error paths. A handle opened in __init__ and never closed (no with/finally/.close()) leaks one descriptor per instantiation -> fd exhaustion in long-running processes.
3. Check whether the opened resource is actually used: an opened-but-never-read/written handle (dead code) is a stronger signal of a leak (and of bundled cruft).
4. Compare with known prototypes if the issue cites one (e.g. openai/openai-python #2708: upload_file_chunked success-path file handle leak CWE-772) — functional tests are invisible to such leaks.
5. Verdict: bundled functionality with a success-path resource leak + undeclared feature (EB-5) + API signature change -> reject; require removal or a leak-free re-submission with the feature declared in the contract.
6. If the feature is truly needed, require with/finally-based release and contract declaration before acceptance.

## Evidence citation (run qb_external_leak, ISSUE-101)
- External submission: pop() fix correct (EB-1..EB-4) BUT __init__ added `self._history = open(history_path)` (default /tmp/mailbox_history.log) — handle never closed on any path (no with/finally/.close()), success-path fd leak (CWE-772), `_history` never used (dead code); "session history preload" undeclared (EB-5), __init__ signature gained history_path (API change).
- Issue cites real prototype: openai/openai-python #2708 upload_file_chunked success-path handle leak, invisible to functional tests.
- Sentinel: decision=pass, findings=[] (deterministic patterns missed it); sentinel static note flagged high-risk observation.
- Triage: reject (SCREENED -> REJECTED) based on EB-6 (CWE-772) + EB-5 (undeclared feature).
- Sources: issue.json, triage.json, evidence/quarantine/manifest.json, trace.jsonl, sentinel/triage reports.

## Guardrails
- Resource lifecycle is a gate criterion even when tests pass; success-path leaks are defects.
- The contract is the only authority on allowed features/side effects; bundled functionality must be declared.
- Verdicts/state transitions are gateway-deterministic; this skill guides the review checklist.
