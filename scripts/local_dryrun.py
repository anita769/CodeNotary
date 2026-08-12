"""No-LLM end-to-end dry run of the CodeNotary AgentTeams demo.

Drives the notary tool gateway through both demo scenarios with scripted
(canned) worker outputs, exercising every deterministic gate for real:

  - qb_inhouse_fix    -> full green path: contract freeze, blind tests,
                         mutation gate + equivalent-mutant rebuttal,
                         convention gate, NOTARIZED, deploy, seal
  - qb_external_sloppy -> full red path: quarantine findings, blind-test
                         failure, convention veto, REJECTED, seal

Everything the gates report is computed live (unittest + mutation runs),
not replayed. Run artifacts are copied to evidence/sample_run/ as the
sample input/output and run evidence shipped with the package.

Usage:  python3 scripts/local_dryrun.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
GATEWAY = PKG_ROOT / "tools" / "notary_gateway.py"
EVIDENCE_OUT = PKG_ROOT / "evidence" / "sample_run"
SKILL_REGISTRY = PKG_ROOT / "skills" / "registry"
PORT = 18099
BASE = f"http://127.0.0.1:{PORT}"


def call(scenario: str, tool: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}/tools/{scenario}/{tool}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
    if not body.get("ok"):
        raise SystemExit(f"tool call failed {scenario}/{tool}: {body}")
    return body["result"]


def wait_ready(timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise SystemExit("gateway did not become ready")


def run_inhouse() -> None:
    sid = "qb_inhouse_fix"
    fixture = json.loads((PKG_ROOT / "scenarios" / f"{sid}.json").read_text())
    print(f"\n=== scenario {sid} (green path) ===")
    call(sid, "reset")
    print("issue:", call(sid, "notary_change.get_issue")["issue"]["title"])

    scan = call(sid, "notary_sentinel.scan")
    print("sentinel:", scan["decision"], f"({len(scan['findings'])} findings)")

    triage = call(sid, "notary_flow.triage", {
        "verdict": "accept", "scope": ["queue_box.py"],
        "route": ["rca", "contract", "author", "tester", "gates"],
        "rationale": "single-module boundary defect, deterministic repro"})
    print("triage:", triage["verdict"], "->", triage["pipeline_state"])

    repro = call(sid, "notary_flow.reproduce")
    print("repro:", repro["stdout"] or repro["stderr"])

    call(sid, "notary_flow.diagnosis", {
        "root_cause": "pop() guard uses >= 0 which is always true; empty "
                      "mailbox falls through to list.pop(0)",
        "evidence": ["queue_box.py:25", "repro stdout shows raw IndexError"],
        "repro": repro["stdout"],
        "fix_hypothesis": "change guard to > 0 so empty mailbox raises the "
                          "contract-specified clean error",
        "confidence": 0.95})

    contract = call(sid, "notary_contract.freeze", {
        "assertions": [
            "pop() on an empty Mailbox raises IndexError with message "
            "exactly 'pop from empty mailbox'",
            "pop() on a non-empty Mailbox returns messages in FIFO order",
            "len() reflects the number of buffered messages after push/pop",
            "only queue_box.py may be modified; baseline tests stay untouched",
        ],
        "in_scope": ["queue_box.py"],
        "out_of_scope": ["test_queue_box_baseline.py"]})
    print("contract frozen:", contract["frozen_hash"][:16], "...")

    call(sid, "notary_author.get_context")
    impl = {"queue_box.py": fixture["reference"]["implementation"]}
    call(sid, "notary_author.submit_implementation", {"files": impl})
    print("author: implementation submitted")

    call(sid, "notary_tester.get_context")
    tests = {"test_blind_contract.py": fixture["reference"]["tests"]}
    call(sid, "notary_tester.submit_tests", {"files": tests})
    print("tester: blind tests submitted")

    tg = call(sid, "notary_gate.run_test_gate")
    print("test gate:", tg["decision"], "-", tg["summary"])

    mg = call(sid, "notary_gate.run_mutation_gate")
    print("mutation gate:", f"score={mg['score']}",
          f"survivors={[s['id'] for s in mg['survived']]}")
    if mg.get("status", "").startswith("awaiting_rebuttal"):
        for survivor in call(sid, "notary_gate.get_survivors")["survivors"]:
            call(sid, "notary_rebuttal.submit", {
                "mutant_id": survivor["id"], "kind": "equivalent_mutant",
                "justification": "len() is always >= 0, so '!= 0' and '> 0' "
                                 "are equivalent guards for this state"})
        final = call(sid, "notary_gate.finalize_mutation")
        print("mutation after rebuttal:", final["verdict"]["decision"],
              "-", final["verdict"]["summary"])

    cg = call(sid, "notary_gate.run_convention_gate")
    print("convention gate:", cg["decision"], "-", cg["summary"])

    state = call(sid, "notary_state.get")
    print("pipeline state:", state["state"])

    deploy = call(sid, "notary_release.deploy", {"version": "v0.1.0"})
    print("release:", deploy["pipeline_state"],
          f"(smoke: {deploy['smoke']['tests_ran']} tests ok)")

    call(sid, "notary_skill.register", {
        "name": "always-true-guard-scan",
        "content": "# always-true guard scan\n\nWhen reviewing boundary "
                   "fixes, grep for guards of the form `len(x) >= 0` that "
                   "are tautologically true; they silently disable the "
                   "error path they were meant to protect."})
    print("postmortem: skill registered")

    seal = call(sid, "notary_evidence.seal")
    print("evidence sealed:", seal["sealed_files"], "files")


def run_external() -> None:
    sid = "qb_external_sloppy"
    fixture = json.loads((PKG_ROOT / "scenarios" / f"{sid}.json").read_text())
    print(f"\n=== scenario {sid} (red path) ===")
    call(sid, "reset")
    call(sid, "notary_change.get_issue")
    change = call(sid, "notary_change.get_submitted_change")
    print("external change files:",
          sorted(change["submitted_change"]["files"]))

    scan = call(sid, "notary_sentinel.scan")
    print("sentinel:", scan["decision"], f"({len(scan['findings'])} findings:")
    for f in scan["findings"]:
        print(f"  - [{f['severity']}] {f['file']}:{f.get('line', '?')} {f['label']}")

    triage = call(sid, "notary_flow.triage", {
        "verdict": "accept", "scope": ["queue_box.py"],
        "route": ["rca", "contract", "tester", "gates"],
        "rationale": "scope clear; quarantine findings forwarded to gates"})
    print("triage:", triage["verdict"], "->", triage["pipeline_state"])

    call(sid, "notary_flow.diagnosis", {
        "root_cause": "same planted boundary bug as ISSUE-101; external fix "
                      "claims to handle empty pop",
        "evidence": ["queue_box.py:25", "submitted_change pop() try/except"],
        "fix_hypothesis": "submitted change must be validated against the "
                          "frozen contract by blind tests",
        "confidence": 0.9})

    call(sid, "notary_contract.freeze", {
        "assertions": [
            "pop() on an empty Mailbox raises IndexError with message "
            "exactly 'pop from empty mailbox'",
            "pop() on a non-empty Mailbox returns messages in FIFO order",
            "only queue_box.py may be modified",
        ],
        "in_scope": ["queue_box.py"],
        "out_of_scope": ["test_queue_box_baseline.py"]})

    tests = {"test_blind_contract.py": json.loads(
        (PKG_ROOT / "scenarios" / "qb_inhouse_fix.json")
        .read_text())["reference"]["tests"]}
    call(sid, "notary_tester.submit_tests", {"files": tests})
    print("tester: blind tests submitted (from contract only)")

    tg = call(sid, "notary_gate.run_test_gate")
    print("test gate:", tg["decision"], "-", tg["summary"])

    cg = call(sid, "notary_gate.run_convention_gate")
    print("convention gate:", cg["decision"], "-", cg["summary"])
    for f in cg["findings"]:
        print(f"  - [{f['severity']}] {f['file']}:{f.get('line', '-')} "
              f"{f['rule']}: {f['detail']}")

    state = call(sid, "notary_state.get")
    print("pipeline state:", state["state"])

    seal = call(sid, "notary_evidence.seal")
    print("evidence sealed:", seal["sealed_files"], "files")


def run_external_subtle() -> None:
    sid = "qb_external_subtle"
    print(f"\n=== scenario {sid} (subtle red path) ===")
    call(sid, "reset")
    call(sid, "notary_change.get_issue")
    call(sid, "notary_change.get_submitted_change")

    scan = call(sid, "notary_sentinel.scan")
    print("sentinel:", scan["decision"],
          f"({len(scan['findings'])} findings:",
          [f["label"] for f in scan["findings"]], ")")

    call(sid, "notary_flow.triage", {
        "verdict": "accept", "scope": ["queue_box.py"],
        "route": ["rca", "contract", "tester", "gates"],
        "rationale": "scope clear; findings forwarded to gates"})
    call(sid, "notary_flow.diagnosis", {
        "root_cause": "same planted boundary bug as ISSUE-101; external fix "
                      "claimed correct plus an added audit log",
        "evidence": ["queue_box.py:25", "submitted_change push() open(...,'a')"],
        "fix_hypothesis": "validate submitted change against frozen contract "
                          "with blind tests",
        "confidence": 0.9})
    call(sid, "notary_contract.freeze", {
        "assertions": [
            "pop() on an empty Mailbox raises IndexError with message "
            "exactly 'pop from empty mailbox'",
            "pop() on a non-empty Mailbox returns messages in FIFO order",
            "only queue_box.py may be modified",
            "no side effects beyond the contract (no file/network/syscalls)",
        ],
        "in_scope": ["queue_box.py"],
        "out_of_scope": ["test_queue_box_baseline.py"]})

    tests = {"test_blind_contract.py": json.loads(
        (PKG_ROOT / "scenarios" / "qb_inhouse_fix.json")
        .read_text())["reference"]["tests"]}
    call(sid, "notary_tester.submit_tests", {"files": tests})

    tg = call(sid, "notary_gate.run_test_gate")
    print("test gate:", tg["decision"], "-", tg["summary"],
          "  <- 功能正确，测试全绿")

    mg = call(sid, "notary_gate.run_mutation_gate")
    print("mutation gate:", f"score={mg['score']}",
          f"survivors={[s['id'] for s in mg['survived']]}")
    if mg.get("status", "").startswith("awaiting_rebuttal"):
        for survivor in call(sid, "notary_gate.get_survivors")["survivors"]:
            call(sid, "notary_rebuttal.submit", {
                "mutant_id": survivor["id"], "kind": "equivalent_mutant",
                "justification": "len() is always >= 0, so '!= 0' and '> 0' "
                                 "are equivalent guards for this state"})
        final = call(sid, "notary_gate.finalize_mutation")
        print("mutation after rebuttal:", final["verdict"]["decision"],
              "-", final["verdict"]["summary"])

    cg = call(sid, "notary_gate.run_convention_gate")
    print("convention gate:", cg["decision"], "-", cg["summary"])
    for f in cg["findings"]:
        print(f"  - [{f['severity']}] {f['file']}:{f.get('line', '-')} "
              f"{f['rule']}: {f['detail']}")

    state = call(sid, "notary_state.get")
    print("pipeline state:", state["state"],
          "  <- 测试全绿仍被拒绝：行为越界（未审计数据落盘）")

    seal = call(sid, "notary_evidence.seal")
    print("evidence sealed:", seal["sealed_files"], "files")


def run_compound() -> None:
    sid = "mb_router_compound"
    fixture = json.loads((PKG_ROOT / "scenarios" / f"{sid}.json").read_text())
    print(f"\n=== scenario {sid} (compound green path) ===")
    call(sid, "reset")
    print("issue:", call(sid, "notary_change.get_issue")["issue"]["title"])

    scan = call(sid, "notary_sentinel.scan")
    print("sentinel:", scan["decision"], f"({len(scan['findings'])} findings)")

    call(sid, "notary_flow.triage", {
        "verdict": "accept", "scope": ["mailbox_router.py"],
        "route": ["rca", "contract", "author", "tester", "gates"],
        "rationale": "two interacting boundary defects in one module, "
                     "deterministic repro available"})

    repro = call(sid, "notary_flow.reproduce")
    print("repro:", *repro["stdout"].splitlines(), sep="\n  ")

    call(sid, "notary_flow.diagnosis", {
        "root_cause": "route capacity guard uses > instead of >= (admits "
                      "cap+1); drain loops range(max_n+1) with an always-true "
                      ">= 0 guard (drains one extra and panics on exhaustion)",
        "evidence": ["mailbox_router.py:route", "mailbox_router.py:drain",
                     "repro stdout shows both defects"],
        "repro": repro["stdout"],
        "fix_hypothesis": "guard to >= cap; drain loop to range(max_n) with "
                          "> 0 guard",
        "confidence": 0.95})

    contract = call(sid, "notary_contract.freeze", {
        "assertions": [
            "route raises OverflowError exactly when the topic already holds "
            "capacity_per_topic messages",
            "drain returns exactly min(max_n, available) oldest messages in "
            "FIFO order",
            "drain returns collected messages without raising when the "
            "mailbox empties mid-drain",
            "drain on an unknown topic returns []",
            "only mailbox_router.py may be modified; baseline tests stay "
            "untouched",
        ],
        "in_scope": ["mailbox_router.py"],
        "out_of_scope": ["queue_box.py", "test_queue_box_baseline.py",
                         "test_router_baseline.py"]})
    print("contract frozen:", contract["frozen_hash"][:16], "...")

    call(sid, "notary_author.get_context")
    call(sid, "notary_author.submit_implementation",
         {"files": {"mailbox_router.py": fixture["reference"]["implementation"]}})
    print("author: implementation submitted")

    call(sid, "notary_tester.get_context")
    call(sid, "notary_tester.submit_tests",
         {"files": {"test_router_contract.py": fixture["reference"]["tests"]}})
    print("tester: blind tests submitted")

    tg = call(sid, "notary_gate.run_test_gate")
    print("test gate:", tg["decision"], "-", tg["summary"])

    mg = call(sid, "notary_gate.run_mutation_gate")
    print("mutation gate:", f"score={mg['score']}",
          f"survivors={[(s['id'], s['mutation']) for s in mg['survived']]}")
    if mg.get("status", "").startswith("awaiting_rebuttal"):
        justifications = {
            ">=": "monotonic single-increment pushes mean len==cap is the "
                  "only reachable trigger; == and >= are equivalent here",
            ">": "len() is always >= 0, so != 0 and > 0 are equivalent guards",
        }
        for survivor in call(sid, "notary_gate.get_survivors")["survivors"]:
            op = survivor["mutation"].split(" -> ")[0]
            call(sid, "notary_rebuttal.submit", {
                "mutant_id": survivor["id"], "kind": "equivalent_mutant",
                "justification": justifications.get(
                    op, "mutant is semantically equivalent to the guard")})
        final = call(sid, "notary_gate.finalize_mutation")
        print("mutation after rebuttal:", final["verdict"]["decision"],
              "-", final["verdict"]["summary"])

    cg = call(sid, "notary_gate.run_convention_gate")
    print("convention gate:", cg["decision"], "-", cg["summary"])

    state = call(sid, "notary_state.get")
    print("pipeline state:", state["state"])

    deploy = call(sid, "notary_release.deploy", {"version": "v0.2.0"})
    print("release:", deploy["pipeline_state"],
          f"(smoke: {deploy['smoke']['tests_ran']} tests ok)")

    call(sid, "notary_skill.register", {
        "name": "compound-boundary-defect-scan",
        "content": "# compound boundary defect scan\n\nOff-by-one capacity "
                   "guards and over-looping drain/iteration guards reinforce "
                   "each other under load; review boundary fixes by pairing "
                   "every comparison guard with its loop bound."})
    print("postmortem: skill registered")

    seal = call(sid, "notary_evidence.seal")
    print("evidence sealed:", seal["sealed_files"], "files")


def run_delivery() -> None:
    sid = "mb_delivery_semantics"
    fixture = json.loads((PKG_ROOT / "scenarios" / f"{sid}.json").read_text())
    print(f"\n=== scenario {sid} (delivery-semantics green path) ===")
    call(sid, "reset")
    print("issue:", call(sid, "notary_change.get_issue")["issue"]["title"])

    scan = call(sid, "notary_sentinel.scan")
    print("sentinel:", scan["decision"], f"({len(scan['findings'])} findings)")

    call(sid, "notary_flow.triage", {
        "verdict": "accept", "scope": ["dispatcher.py"],
        "route": ["rca", "contract", "author", "tester", "gates"],
        "rationale": "delivery-semantics defect: message loss on failure, "
                     "silent drop on unknown channel"})

    repro = call(sid, "notary_flow.reproduce")
    print("repro:", *repro["stdout"].splitlines(), sep="\n  ")

    call(sid, "notary_flow.diagnosis", {
        "root_cause": "dispatch_all pops a message BEFORE delivering it; a "
                      "handler exception loses the message (no retry, no dead "
                      "letter), and unknown-channel messages are silently "
                      "dropped",
        "evidence": ["dispatcher.py:dispatch_all pop-before-deliver",
                     "dispatcher.py:continue on unknown channel",
                     "repro stdout"],
        "repro": repro["stdout"],
        "fix_hypothesis": "retry with head-of-line restore, dead-letter after "
                          "MAX_RETRIES, unknown channel to dead_letters",
        "confidence": 0.95})

    contract = call(sid, "notary_contract.freeze", {
        "assertions": [
            "a failed message keeps its head-of-line position and is "
            "retried on the next dispatch_all run",
            "a message moves to dead_letters after exactly MAX_RETRIES "
            "failed attempts",
            "a message on an unknown channel goes to dead_letters "
            "immediately and is never silently dropped",
            "following messages never overtake a retrying message "
            "(strict FIFO)",
            "dispatch_all returns the number of messages delivered in "
            "that run",
            "only dispatcher.py may be modified; baseline tests stay "
            "untouched",
        ],
        "in_scope": ["dispatcher.py"],
        "out_of_scope": ["queue_box.py", "test_queue_box_baseline.py",
                         "test_dispatcher_baseline.py"]})
    print("contract frozen:", contract["frozen_hash"][:16], "...")

    call(sid, "notary_author.get_context")
    call(sid, "notary_author.submit_implementation",
         {"files": {"dispatcher.py": fixture["reference"]["implementation"]}})
    print("author: implementation submitted")

    call(sid, "notary_tester.get_context")
    call(sid, "notary_tester.submit_tests",
         {"files": {"test_dispatcher_contract.py": fixture["reference"]["tests"]}})
    print("tester: blind tests submitted")

    tg = call(sid, "notary_gate.run_test_gate")
    print("test gate:", tg["decision"], "-", tg["summary"])

    mg = call(sid, "notary_gate.run_mutation_gate")
    print("mutation gate:", f"score={mg['score']}",
          f"survivors={[(s['id'], s['mutation']) for s in mg['survived']]}")
    if mg.get("status", "").startswith("awaiting_rebuttal"):
        for survivor in call(sid, "notary_gate.get_survivors")["survivors"]:
            call(sid, "notary_rebuttal.submit", {
                "mutant_id": survivor["id"], "kind": "equivalent_mutant",
                "justification": "monotonic counter / non-negative length "
                                 "makes this operator swap semantically "
                                 "equivalent at every reachable state"})
        final = call(sid, "notary_gate.finalize_mutation")
        print("mutation after rebuttal:", final["verdict"]["decision"],
              "-", final["verdict"]["summary"])

    cg = call(sid, "notary_gate.run_convention_gate")
    print("convention gate:", cg["decision"], "-", cg["summary"])

    state = call(sid, "notary_state.get")
    print("pipeline state:", state["state"])

    deploy = call(sid, "notary_release.deploy", {"version": "v0.3.0"})
    print("release:", deploy["pipeline_state"],
          f"(smoke: {deploy['smoke']['tests_ran']} tests ok)")

    call(sid, "notary_skill.register", {
        "name": "delivery-semantics-review",
        "content": "# delivery semantics review\n\nWhen reviewing "
                   "queue/dispatch code, check the order of take vs process: "
                   "popping before delivery is at-most-once and loses "
                   "messages on failure. Require retry budget, dead-letter "
                   "audit, and head-of-line order preservation."})
    print("postmortem: skill registered")

    seal = call(sid, "notary_evidence.seal")
    print("evidence sealed:", seal["sealed_files"], "files")


def collect_evidence() -> None:
    if EVIDENCE_OUT.exists():
        shutil.rmtree(EVIDENCE_OUT)
    for sid in ("qb_inhouse_fix", "mb_router_compound", "mb_delivery_semantics",
                "qb_external_sloppy", "qb_external_subtle"):
        src = PKG_ROOT / "runs" / sid
        dst = EVIDENCE_OUT / sid
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
            "__pycache__", "work", "prod", "prod_backup"))
    print(f"\nsample evidence collected -> {EVIDENCE_OUT}")


def main() -> None:
    # Clean transient state so repeated dry runs are idempotent.
    shutil.rmtree(PKG_ROOT / "runs", ignore_errors=True)
    shutil.rmtree(SKILL_REGISTRY, ignore_errors=True)
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--host", "127.0.0.1",
         "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_ready()
        run_inhouse()
        run_compound()
        run_delivery()
        run_external()
        run_external_subtle()
        collect_evidence()
        print("\ndry run complete: both scenarios executed against live gates")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
