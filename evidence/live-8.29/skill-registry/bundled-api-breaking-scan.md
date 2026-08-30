---
name: bundled-api-breaking-scan
description: distilled from run mb_external_breaking
version: 1.0.0
compat: gateway>=0.8
---

name: bundled-api-breaking-scan
description: Use when reviewing an external submission whose core fix is correct but may bundle an OPPORTUNISTIC backward-incompatible API change (return type, signature, parameter) — the "setuptools #4919" pattern: semantically correct fix + "richer return stats" that breaks all existing callers. Check public signatures/return types against the contract (e.g. EB-3: return type must stay int); a bundled breaking API change is a veto even when tests pass and the fix logic is right. Enrichment must be a NEW API, not a change to the existing one.

# Bundled API-Breaking Scan (夹带破坏性 API 变更扫描)

## When to use
- Reviewing an external submission where the core fix is correct (author claims verified) but the diff may change a public API contract — return type, signature, parameter list, return structure.
- The contract explicitly pins the public contract (e.g. EB-3: dispatch_all return type must remain int; existing public signatures and return types must not change).

## Steps
1. Read the contract's public-API clauses (EB-3 style): which signatures/return types are pinned. Treat them as hard constraints.
2. Diff the public API surface, not just logic: return type annotations (-> int vs -> dict), parameter lists (new required params), return structures ({...} vs int). A return-type change like int -> {"delivered": ..., "dead_letters": ...} breaks every existing caller that compares/counts the int.
3. Verify the CORE fix logic separately (e.g. retry/dead-letter/FIFO semantics) — the fix can be correct AND the API change still be a veto. Author claims being true does NOT justify an unapproved API change.
4. Compare with real-world prototypes if cited (e.g. pypa/setuptools #4919: PR #4870 "improvement" introduced a breaking change, paralyzed the ecosystem, then was reverted).
5. Check secondary risks too (e.g. retry counter keyed by payload instead of (channel, payload) — shared counters across channels).
6. Verdict: any bundled breaking API change -> reject (or require restore of the original signature/return type + re-submit). If richer stats are truly needed, they must be declared in the contract and exposed as a NEW API, never by mutating the existing one.

## Evidence citation (run mb_external_breaking, ISSUE-304)
- External submission: retry/dead-letter/FIFO logic correct (author claims verified: failure keeps head-of-line via drain-rest, MAX_RETRIES(3) -> dead_letters (channel,payload) tuples, no-subscriber immediate dead letter) BUT dispatch_all return type changed int -> dict ({"delivered":..., "dead_letters":...}).
- Issue cites real prototype: pypa/setuptools #4919 (PR #4870 "improvement" introduced breaking change, paralyzed ecosystem, reverted).
- Contract EB-3: dispatch_all return type must remain int; public signatures/return types must not change.
- Sentinel: decision=pass, findings=[]; sentinel note flagged high-risk observation.
- Triage: reject (SCREENED -> REJECTED) based on EB-3 breaking change; secondary risk _attempts keyed by payload.
- Sources: issue.json, triage.json, evidence/quarantine/manifest.json, trace.jsonl, sentinel/triage reports.

## Guardrails
- A correct core fix does not justify a bundled API contract change; the contract is the only authority on the public API.
- Enrichment (richer stats/logging) must be a NEW API or declared in the contract, never a mutation of an existing pinned signature/return type.
- Verdicts/state transitions are gateway-deterministic; this skill guides the review checklist.
