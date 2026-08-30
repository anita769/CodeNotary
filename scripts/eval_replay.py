"""Deterministic evaluation replay for the CodeNotary evalset.

Drives every sample in evalset/manifest.json through the REAL notary
gateway (no LLM, canned worker outputs taken from the manifest's replay
parameters), exercises every deterministic gate for real — unittest
execution, mutation injection, convention scanning, state machine — and
compares the actual per-gate decision sequence against the published
expectation for each sample.

Output: evalset/results.json — canonical (timestamp-free, sorted-keys)
JSON so repeated runs are bit-identical. Run it twice and compare hashes
to verify the "same input, same output" property empirically.

Usage:  python3 scripts/eval_replay.py
Exit:   0 iff every replayed sample matches its published expectation.
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
EVALSET = PKG_ROOT / "evalset"
SKILL_REGISTRY = PKG_ROOT / "skills" / "registry"
MATCH_TABLE = PKG_ROOT / "skills" / "match_table.json"
PORT = 18098
BASE = f"http://127.0.0.1:{PORT}"


class EvalFailure(Exception):
    pass


def call(scenario: str, tool: str, payload: dict | None = None,
         role: str | None = None) -> dict:
    body_payload = dict(payload or {})
    if role:
        body_payload["role"] = role
    req = urllib.request.Request(
        f"{BASE}/tools/{scenario}/{tool}",
        data=json.dumps(body_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
    if not body.get("ok"):
        raise EvalFailure(f"tool call failed {scenario}/{tool}: {body}")
    return body["result"]


def signals_by_sample() -> dict[str, list[dict]]:
    """Reverse map: evalset sample id -> decision-table signals whose
    coverage list names it. Drives the runtime skill consultation that
    workers perform via notary_skill.match during replay."""
    table = json.loads(MATCH_TABLE.read_text(encoding="utf-8"))
    by_sample: dict[str, list[dict]] = {}
    for entry in table["signals"]:
        for sample_id in entry.get("coverage", []):
            by_sample.setdefault(sample_id, []).append(entry)
    return by_sample


def load_scenario(sid: str) -> dict:
    return json.loads(
        (PKG_ROOT / "scenarios" / f"{sid}.json").read_text(encoding="utf-8"))


def blind_tests(sample: dict) -> dict:
    src = sample["replay"]["tests_source"]
    origin = sample["scenario"] if src["scenario"] == "self" else src["scenario"]
    fixture = load_scenario(origin)
    return {src["filename"]: fixture["reference"]["tests"]}


def drive_to_gates(sample: dict) -> None:
    sid = sample["scenario"]
    rp = sample["replay"]
    call(sid, "reset")
    call(sid, "notary_change.get_issue", role="triage")
    if load_scenario(sid)["mode"] == "external":
        call(sid, "notary_change.get_submitted_change", role="triage")


def do_sentinel(sample: dict, actual: dict) -> None:
    sid = sample["scenario"]
    scan = call(sid, "notary_sentinel.scan", role="sentinel")
    actual["sentinel"] = scan["decision"]
    actual["sentinel_findings"] = len(scan["findings"])
    actual["sentinel_critical_rules"] = sorted(
        {f["label"] for f in scan["findings"] if f["severity"] == "critical"})


def do_skill_consult(sample: dict, actual: dict) -> None:
    """Workers consult the Skill runtime: match the trigger signal, then
    fetch the matched skill — the same two-call pattern Agent.md instructs.
    Statuses are recorded verbatim (a registry skill matched before its
    distillation run reports not_yet_registered — deterministic because
    the manifest order is fixed)."""
    sid = sample["scenario"]
    matches: dict[str, str] = {}
    for entry in signals_by_sample().get(sample["id"], []):
        roles = entry.get("roles") or ["author"]
        role = "tester" if "tester" in roles else roles[0]
        res = call(sid, "notary_skill.match",
                   {"signal": entry["signal"]}, role=role)
        matches[entry["signal"]] = f"{res['status']}:{res['skill']}"
        if res["status"] == "matched":
            call(sid, "notary_skill.get", {"name": res["skill"]}, role=role)
    if matches:
        actual["skill_matches"] = matches


def do_flow(sample: dict) -> None:
    sid = sample["scenario"]
    rp = sample["replay"]
    call(sid, "notary_flow.triage", {
        "verdict": "accept", "scope": rp["scope"],
        "route": ["rca", "contract", "author", "tester", "gates"],
        "rationale": "eval replay: scope clear; findings forwarded to gates"},
        role="triage")
    fixture = load_scenario(sid)
    repro_out = ""
    if fixture["mode"] == "inhouse":
        repro_out = call(sid, "notary_flow.reproduce", role="rca").get(
            "stdout", "")
    call(sid, "notary_flow.diagnosis", {
        "root_cause": rp["diagnosis"]["root_cause"],
        "evidence": ["eval replay"],
        "repro": repro_out,
        "fix_hypothesis": rp["diagnosis"]["fix_hypothesis"],
        "confidence": 0.95}, role="rca")
    call(sid, "notary_contract.freeze", {
        "assertions": rp["contract_assertions"],
        "in_scope": rp["scope"],
        "out_of_scope": rp.get("out_of_scope", [])}, role="contract")


def do_mutation(sample: dict, actual: dict) -> None:
    sid = sample["scenario"]
    mg = call(sid, "notary_gate.run_mutation_gate", role="gatekeeper")
    if mg.get("status", "").startswith("awaiting_rebuttal"):
        justs = sample["replay"].get("rebuttal_justifications", {})
        for survivor in call(sid, "notary_gate.get_survivors",
                             role="author")["survivors"]:
            op = survivor["mutation"].split(" -> ")[0]
            call(sid, "notary_rebuttal.submit", {
                "mutant_id": survivor["id"], "kind": "equivalent_mutant",
                "justification": justs.get(
                    op, justs.get("default",
                                  "mutant is semantically equivalent"))},
                role="author")
        final = call(sid, "notary_gate.finalize_mutation", role="gatekeeper")
        verdict = final["verdict"]
    else:
        verdict = mg.get("verdict", {})
    actual["mutation_gate"] = verdict.get("decision")
    actual["mutation_score"] = verdict.get("score")


def run_sample(sample: dict) -> dict:
    sid = sample["scenario"]
    path = sample["path"]
    actual: dict = {"sentinel": "skipped", "test_gate": "skipped",
                    "mutation_gate": "skipped", "convention_gate": "skipped",
                    "final": None}

    if path == "external_quarantine":
        drive_to_gates(sample)
        do_sentinel(sample, actual)
        do_skill_consult(sample, actual)
        call(sid, "notary_flow.triage", {
            "verdict": "reject", "scope": sample["replay"]["scope"],
            "route": [],
            "rationale": "critical quarantine finding: unconditional reject"},
            role="triage")
        actual["final"] = call(sid, "notary_state.get")["state"]
        call(sid, "notary_evidence.seal", role="release")
        return actual

    drive_to_gates(sample)
    do_sentinel(sample, actual)
    do_flow(sample)
    do_skill_consult(sample, actual)

    fixture = load_scenario(sid)
    if fixture["mode"] == "inhouse":
        call(sid, "notary_author.get_context", role="author")
        call(sid, "notary_author.submit_implementation", {
            "files": {rp_scope_to_file(sample):
                      fixture["reference"]["implementation"]}}, role="author")
    call(sid, "notary_tester.get_context", role="tester")
    call(sid, "notary_tester.submit_tests", {"files": blind_tests(sample)},
         role="tester")

    tg = call(sid, "notary_gate.run_test_gate", role="gatekeeper")
    actual["test_gate"] = tg["decision"]

    if tg["decision"] == "green":
        do_mutation(sample, actual)

    cg = call(sid, "notary_gate.run_convention_gate", role="gatekeeper")
    actual["convention_gate"] = cg["decision"]
    actual["convention_findings"] = len(cg["findings"])
    actual["convention_veto"] = sum(
        1 for f in cg["findings"] if f["severity"] == "veto")
    actual["convention_rules"] = sorted(
        {f["rule"] for f in cg["findings"] if f["severity"] == "veto"})

    if path == "inhouse_green":
        state = call(sid, "notary_state.get")["state"]
        if state == "NOTARIZED":
            call(sid, "notary_release.deploy",
                 {"version": sample["replay"]["version"]}, role="release")
            # Postmortem distillation is optional per sample: D5 vendored
            # real-repo scenarios don't mint new skills (the registry's
            # orphan audit requires every skill be signal-reachable).
            pm = sample["replay"].get("postmortem")
            if pm:
                call(sid, "notary_skill.register",
                     {"name": pm["name"], "content": pm["content"]},
                     role="postmortem")
        actual["final"] = call(sid, "notary_state.get")["state"]
    else:
        actual["final"] = call(sid, "notary_state.get")["state"]
        # Red-path postmortem: a rejection is not the end — the pipeline
        # distills the lesson it just enforced into a new registry skill.
        pm = sample["replay"].get("postmortem")
        if pm and actual["final"] == "REJECTED":
            call(sid, "notary_skill.register",
                 {"name": pm["name"], "content": pm["content"]},
                 role="postmortem")

    call(sid, "notary_evidence.seal", role="release")
    return actual


def rp_scope_to_file(sample: dict) -> str:
    return sample["replay"]["scope"][0]


def _record_convention(sample: dict, actual: dict) -> None:
    sid = sample["scenario"]
    cg = call(sid, "notary_gate.run_convention_gate", role="gatekeeper")
    actual["convention_gate"] = cg["decision"]
    actual["convention_findings"] = len(cg["findings"])
    actual["convention_veto"] = sum(
        1 for f in cg["findings"] if f["severity"] == "veto")
    actual["convention_rules"] = sorted(
        {f["rule"] for f in cg["findings"] if f["severity"] == "veto"})


def _finish_green(sample: dict, actual: dict, fixture: dict) -> None:
    """Shared green leg: (re)submit impl -> gates -> deploy -> seal."""
    sid = sample["scenario"]
    rp = sample["replay"]
    fname = rp_scope_to_file(sample)
    impl = (rp.get("rework", {}) or {}).get("impl_v2") \
        or fixture["reference"]["implementation"]
    call(sid, "notary_author.submit_implementation",
         {"files": {fname: impl}}, role="author")
    tg = call(sid, "notary_gate.run_test_gate", role="gatekeeper")
    actual["test_gate"] = tg["decision"]
    if tg["decision"] == "green":
        do_mutation(sample, actual)
    _record_convention(sample, actual)
    if call(sid, "notary_state.get")["state"] == "NOTARIZED":
        call(sid, "notary_release.deploy",
             {"version": rp["version"]}, role="release")
    actual["final"] = call(sid, "notary_state.get")["state"]
    call(sid, "notary_evidence.seal", role="release")


def run_rework(sample: dict) -> dict:
    """L2+L3 rework loop: mutation survivors -> accept_fix -> yellow ->
    ESCALATED -> human approves rework -> author revises -> green."""
    sid = sample["scenario"]
    rp = sample["replay"]
    rw = rp["rework"]
    actual: dict = {"sentinel": "skipped", "test_gate": "skipped",
                    "first_mutation": "skipped", "mutation_gate": "skipped",
                    "convention_gate": "skipped",
                    "escalated_to_gating": False, "final": None}
    drive_to_gates(sample)
    do_sentinel(sample, actual)
    do_flow(sample)
    do_skill_consult(sample, actual)
    fixture = load_scenario(sid)
    call(sid, "notary_author.get_context", role="author")
    call(sid, "notary_author.submit_implementation",
         {"files": {rp_scope_to_file(sample): rw["impl_v1"]}}, role="author")
    call(sid, "notary_tester.get_context", role="tester")
    call(sid, "notary_tester.submit_tests", {"files": blind_tests(sample)},
         role="tester")
    tg = call(sid, "notary_gate.run_test_gate", role="gatekeeper")
    actual["test_gate"] = tg["decision"]
    if tg["decision"] != "green":
        actual["final"] = call(sid, "notary_state.get")["state"]
        return actual

    call(sid, "notary_gate.run_mutation_gate", role="gatekeeper")
    survivors = call(sid, "notary_gate.get_survivors",
                     role="author")["survivors"]
    for s in survivors:
        spec = next((r for r in rw["round1_rebuttals"]
                     if r["mutation_contains"] in s["mutation"]), None)
        if spec is None:
            raise EvalFailure(f"no rebuttal spec for survivor {s}")
        call(sid, "notary_rebuttal.submit",
             {"mutant_id": s["id"], "kind": spec["kind"],
              "justification": spec["justification"]}, role="author")
    final1 = call(sid, "notary_gate.finalize_mutation", role="gatekeeper")
    actual["first_mutation"] = final1["verdict"]["decision"]
    if actual["first_mutation"] == "yellow":
        # Human arbitration: rework approved, back to GATING for the
        # revised implementation (L3 first exit).
        call(sid, "notary_flow.resolve_human", {"approve": True},
             role="leader")
        actual["escalated_to_gating"] = (
            call(sid, "notary_state.get")["state"] == "GATING")
        _finish_green(sample, actual, fixture)
    else:
        actual["final"] = call(sid, "notary_state.get")["state"]
        call(sid, "notary_evidence.seal", role="release")
    return actual


def run_red_rework(sample: dict) -> dict:
    """L4 bounded rework loop: test gate red -> REJECTED ->
    request_rework -> author fixes -> all green -> RELEASED."""
    sid = sample["scenario"]
    rp = sample["replay"]
    rw = rp["rework"]
    actual: dict = {"sentinel": "skipped", "first_test_gate": "skipped",
                    "first_final": None, "rework_round": 0,
                    "test_gate": "skipped", "mutation_gate": "skipped",
                    "convention_gate": "skipped", "final": None}
    drive_to_gates(sample)
    do_sentinel(sample, actual)
    do_flow(sample)
    do_skill_consult(sample, actual)
    fixture = load_scenario(sid)
    fname = rp_scope_to_file(sample)
    impl_v1 = rw["impl_v1"]
    if impl_v1 == "@target":  # the original buggy target source
        impl_v1 = {name: (PKG_ROOT / "tools" / "notary_target" / name
                          ).read_text(encoding="utf-8")
                   for name in fixture["target_files"]}[fname]
    call(sid, "notary_author.get_context", role="author")
    call(sid, "notary_author.submit_implementation",
         {"files": {fname: impl_v1}}, role="author")
    call(sid, "notary_tester.get_context", role="tester")
    call(sid, "notary_tester.submit_tests", {"files": blind_tests(sample)},
         role="tester")
    tg = call(sid, "notary_gate.run_test_gate", role="gatekeeper")
    actual["first_test_gate"] = tg["decision"]
    actual["first_final"] = call(sid, "notary_state.get")["state"]
    if actual["first_final"] != "REJECTED":
        actual["final"] = actual["first_final"]
        return actual
    rwk = call(sid, "notary_flow.request_rework",
               {"reason": rw["rework_reason"]}, role="leader")
    actual["rework_round"] = rwk["rework_round"]
    _finish_green(sample, actual, fixture)
    # L5: the release is itself reversible — exercise the rollback edge so
    # every retreat edge in the state machine has replay evidence.
    if actual["final"] == "RELEASED":
        rb = call(sid, "notary_release.rollback", role="release")
        actual["rollback_restored"] = rb["restored_backup"]
        actual["prod_removed"] = rb["prod_removed"]
        actual["final"] = call(sid, "notary_state.get")["state"]
        call(sid, "notary_evidence.seal", role="release")
    return actual


def run_contract_evolution(sample: dict) -> dict:
    """L3 third exit: ambiguous contract v1 -> escalation -> revised
    contract v2 (hash-chained) -> pipeline proceeds -> RELEASED."""
    sid = sample["scenario"]
    rp = sample["replay"]
    actual: dict = {"sentinel": "skipped", "contract_versions": [],
                    "escalated": False, "test_gate": "skipped",
                    "mutation_gate": "skipped", "convention_gate": "skipped",
                    "final": None}
    drive_to_gates(sample)
    do_sentinel(sample, actual)
    # flow, but freeze the AMBIGUOUS v1 contract first
    call(sid, "notary_flow.triage", {
        "verdict": "accept", "scope": rp["scope"],
        "route": ["rca", "contract", "author", "tester", "gates"],
        "rationale": "eval replay: scope clear"}, role="triage")
    fixture = load_scenario(sid)
    repro_out = ""
    if fixture["mode"] == "inhouse":
        repro_out = call(sid, "notary_flow.reproduce", role="rca").get(
            "stdout", "")
    call(sid, "notary_flow.diagnosis", {
        "root_cause": rp["diagnosis"]["root_cause"], "evidence": ["eval replay"],
        "repro": repro_out,
        "fix_hypothesis": rp["diagnosis"]["fix_hypothesis"],
        "confidence": 0.95}, role="rca")
    f1 = call(sid, "notary_contract.freeze", {
        "assertions": rp["ambiguous_assertions"], "in_scope": rp["scope"],
        "out_of_scope": rp.get("out_of_scope", [])}, role="contract")
    actual["contract_versions"].append(f1["version"])
    # contract role flags the ambiguity in the room; the leader escalates
    # instead of letting two workers diverge on it
    call(sid, "notary_flow.triage", {
        "verdict": "escalate", "scope": rp["scope"], "route": ["leader"],
        "rationale": "contract v1 wording admits multiple interpretations; "
                     "escalating for clarification before workers diverge"},
        role="leader")
    actual["escalated"] = (
        call(sid, "notary_state.get")["state"] == "ESCALATED")
    # arbitration resolves the wording; contract v2 chains to v1
    f2 = call(sid, "notary_contract.freeze", {
        "assertions": rp["contract_assertions"], "in_scope": rp["scope"],
        "out_of_scope": rp.get("out_of_scope", [])}, role="contract")
    actual["contract_versions"].append(f2["version"])
    actual["contract_chained"] = bool(f2["revised"])
    do_skill_consult(sample, actual)
    call(sid, "notary_author.get_context", role="author")
    call(sid, "notary_tester.get_context", role="tester")
    call(sid, "notary_tester.submit_tests", {"files": blind_tests(sample)},
         role="tester")
    _finish_green(sample, actual, fixture)
    return actual


def compare(sample: dict, actual: dict) -> list[str]:
    mismatches = []
    for key, expected in sample["expect"].items():
        if key == "final" and " " in str(expected):
            continue  # prose expectation (live_only samples)
        got = actual.get(key)
        if got != expected:
            mismatches.append(f"{key}: expected {expected!r}, got {got!r}")
    # D3 injected samples: the expected RULE must be the one that fired —
    # a red gate for the wrong reason is a detection-quality failure.
    inj = sample.get("injection")
    if inj:
        rule = inj["expected_rule"]
        if rule == "sentinel-critical":
            if not actual.get("sentinel_critical_rules"):
                mismatches.append("expected a critical sentinel finding")
        elif rule not in actual.get("convention_rules", []):
            mismatches.append(
                f"expected rule {rule!r} in convention vetoes, "
                f"got {actual.get('convention_rules')}")
    return mismatches


def main() -> None:
    manifest = json.loads(
        (EVALSET / "manifest.json").read_text(encoding="utf-8"))
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--host", "127.0.0.1", "--port",
         str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = []
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"{BASE}/health", timeout=1):
                    break
            except Exception:
                time.sleep(0.2)
        # Idempotency: postmortem registry is append-only (re-registering a
        # name is rejected), so replay starts from a clean registry, exactly
        # like scripts/local_dryrun.py does.
        shutil.rmtree(SKILL_REGISTRY, ignore_errors=True)

        for sample in manifest["samples"]:
            if sample.get("live_only"):
                print(f"-- {sample['id']:38s} SKIP (live only)")
                results.append({
                    "id": sample["id"], "source": sample["source"],
                    "scenario": sample["scenario"], "path": sample["path"],
                    "replayed": False, "ok": True, "mismatches": [],
                    "note": "live-only sample (ESCALATED demo)"})
                continue
            path = sample["path"]
            if path == "inhouse_rework":
                actual = run_rework(sample)
            elif path == "inhouse_red_rework":
                actual = run_red_rework(sample)
            elif path == "contract_evolution":
                actual = run_contract_evolution(sample)
            else:
                actual = run_sample(sample)
            mismatches = compare(sample, actual)
            # Every sealed run must carry its certificate (F1): the
            # notary office's namesake deliverable.
            cert = PKG_ROOT / "runs" / sample["scenario"] / "certificate.md"
            if not (cert.exists() and "公证书" in cert.read_text(
                    encoding="utf-8")):
                mismatches.append("certificate.md missing/invalid after seal")
            ok = not mismatches
            mark = "PASS" if ok else "FAIL"
            print(f"{mark} {sample['id']:38s} final={actual['final']}")
            for m in mismatches:
                print(f"     mismatch: {m}")
            results.append({
                "id": sample["id"], "source": sample["source"],
                "scenario": sample["scenario"], "path": sample["path"],
                "replayed": True, "ok": ok,
                "actual": actual, "mismatches": mismatches})
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    replayed = [r for r in results if r["replayed"]]
    passed = [r for r in replayed if r["ok"]]
    by_source: dict[str, dict[str, int]] = {}
    for r in replayed:
        bucket = by_source.setdefault(r["source"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(r["ok"])

    summary = {
        "evalset_version": manifest["version"],
        "samples_total": len(results),
        "samples_replayed": len(replayed),
        "samples_passed": len(passed),
        "terminal_verdict_accuracy": (
            f"{len(passed)}/{len(replayed)}"),
        "by_source": by_source,
    }
    out = {"summary": summary, "results": results}
    (EVALSET / "results.json").write_text(
        json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")

    print("\n===== eval summary =====")
    for src, b in sorted(by_source.items()):
        print(f"  {src:6s} {b['passed']}/{b['total']} passed")
    print(f"  TOTAL  {len(passed)}/{len(replayed)} "
          f"(+{len(results) - len(replayed)} live-only skipped)")
    print(f"results -> {EVALSET / 'results.json'}")
    if len(passed) != len(replayed):
        sys.exit(1)
    print("ALL SAMPLES MATCH PUBLISHED EXPECTATIONS")


if __name__ == "__main__":
    main()
