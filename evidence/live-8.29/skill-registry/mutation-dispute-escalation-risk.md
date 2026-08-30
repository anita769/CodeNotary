---
name: mutation-dispute-escalation-risk
description: distilled from run mb_delivery_semantics
version: 1.0.0
compat: gateway>=0.8
---

name: mutation-dispute-escalation-risk
description: Use when a surviving mutant is a REAL behavior change and you consider disputing it as a test gap — be aware that a dispute triggers ESCALATED (human review) and may end in REJECTED if the human does not adopt the test-gap argument. The deterministic low-risk path is prevention: the killing assertion (exact boundary, handler call count == MAX_RETRIES, no extra attempt) must already be part of the blind suite submitted BEFORE gates run; or fix/rework. Never rely on a post-hoc dispute to exempt a real-behavior mutant.

# Mutation Dispute Escalation Risk (变异 dispute 升级风险)

## When to use
- A mutation gate has a surviving mutant that is a REAL behavior change (not equivalent), and you (author) are considering submitting a dispute claiming it is a test gap.
- You are the coordinator deciding how to handle a dispute.

## Steps
1. Classify the survivor first: equivalent (exempt via rebuttal) vs real behavior change (test gap or implementation defect). Only real-behavior mutants can be disputed.
2. Before disputing, verify the killing assertion is ALREADY part of the blind suite that ran in the gate (e.g. handler call count == MAX_RETRIES, no 4th attempt). If the suite lacked it, the dispute is a post-hoc test-gap claim: it triggers ESCALATED (MUTATION yellow, human review required) and the human may NOT adopt it -> REJECTED.
3. Preferred paths, in order:
   a. Prevention: ensure the blind suite with exact boundary assertions is submitted BEFORE gates run (dual-track merge) so the mutant is killed deterministically.
   b. Rework: on REJECTED, reset/rework with the strengthened suite (see mutation-red-rework-recovery).
   c. Dispute: only as a last resort, with the killing test present in the run as evidence; be aware the outcome is human-decided and may be REJECTED.
4. If the pipeline reaches ESCALATED due to a dispute, do not submit more rebuttals/tests (submit_tests is not valid in ESCALATED); wait for human review or reset.

## Evidence citation (run mb_delivery_semantics, ISSUE-301)
- MUTATION: 6 mutants, 3 killed / 3 survived (M03 `> -> !=`, M05 `>= -> >`, M06 `>= -> ==`), score 0.50.
- Author rebuttals: M03/M06 equivalent, M05 dispute (real behavior change, test gap: TEST_PASS ran only 4 baseline tests, blind suite not in the gate).
- finalize -> MUTATION yellow -> ESCALATED ("author disputed survivor classification; human review required"); human review did NOT adopt the dispute -> REJECTED.
- The blind suite (21 cases incl. test_exactly_three_attempts_no_fourth) was ready but could not be submitted in ESCALATED.
- Contrast: mb_delivery_semantics_ambiguous rework succeeded when the strengthened suite was submitted before gates (M05 killed, score 1.00).
- Sources: verdicts/mutation.json, survivors.md, rebuttals.json, trace.jsonl, state history.

## Guardrails
- A dispute is not an exemption; it escalates to human review and can end REJECTED.
- Never dispute a real-behavior mutant without the killing assertion being part of the gate run; post-hoc test-gap claims are weak.
- Verdicts/state transitions are gateway-deterministic; this skill only guides the decision path.
