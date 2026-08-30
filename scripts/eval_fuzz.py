"""Fail-closed fuzz suite for the notary gateway.

Fires malformed inputs, illegal state transitions and adversarial artifacts
at the REAL gateway and asserts the one property that matters for a
notary: every anomaly is funneled to an explicit error or a rejection —
never a silent pass, never a crash.

Output: evalset/fuzz_results.json (canonical JSON; timestamp-free).

Usage:  python3 scripts/eval_fuzz.py
Exit:   0 iff every case is fail-closed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
GATEWAY = PKG_ROOT / "tools" / "notary_gateway.py"
EVALSET = PKG_ROOT / "evalset"
PORT = 18097
BASE = f"http://127.0.0.1:{PORT}"

results: list[dict] = []


def raw_call(scenario: str, tool: str, payload: dict | None = None,
             raw_body: bytes | None = None) -> tuple[bool, dict]:
    """Call without raising; returns (ok, body)."""
    data = raw_body if raw_body is not None else json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"{BASE}/tools/{scenario}/{tool}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok")), body
    except urllib.error.HTTPError as exc:
        try:
            return False, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return False, {"error": f"HTTP {exc.code} (non-JSON body)"}
    except Exception as exc:  # connection refused etc. => gateway crashed
        return False, {"error": f"CONNECTION FAILURE: {exc}"}


def must(scenario: str, tool: str, payload: dict | None = None) -> dict:
    ok, body = raw_call(scenario, tool, payload)
    if not ok:
        raise SystemExit(f"setup call failed {scenario}/{tool}: {body}")
    return body["result"]


def gateway_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/health", timeout=3):
            return True
    except Exception:
        return False


def record(case_id: str, category: str, expect: str, ok: bool,
           detail: str) -> None:
    verdict = "fail-closed" if ok else "FAIL-OPEN"
    results.append({"id": case_id, "category": category, "expect": expect,
                    "verdict": verdict, "detail": detail})
    mark = "✅" if ok else "❌"
    print(f"{mark} [{category}] {case_id}: {verdict} — {detail[:90]}")


def expect_error(case_id: str, category: str, scenario: str, tool: str,
                 payload: dict | None = None,
                 raw_body: bytes | None = None) -> None:
    ok, body = raw_call(scenario, tool, payload, raw_body)
    good = (not ok) and bool(body.get("error")) and gateway_alive()
    record(case_id, category, "explicit error, gateway alive",
           good, str(body.get("error", body))[:90])


def replay_to_contract(sid: str) -> None:
    """Drive an inhouse scenario up to CONTRACTED (for artifact fuzz)."""
    manifest = json.loads(
        (EVALSET / "manifest.json").read_text(encoding="utf-8"))
    sample = next(s for s in manifest["samples"] if s["scenario"] == sid)
    rp = sample["replay"]
    must(sid, "reset")
    must(sid, "notary_change.get_issue")
    must(sid, "notary_sentinel.scan")
    must(sid, "notary_flow.triage", {
        "verdict": "accept", "scope": rp["scope"],
        "route": ["rca", "contract", "author", "tester", "gates"],
        "rationale": "fuzz setup"})
    repro = must(sid, "notary_flow.reproduce")
    must(sid, "notary_flow.diagnosis", {
        "root_cause": rp["diagnosis"]["root_cause"], "evidence": ["fuzz"],
        "repro": repro.get("stdout", ""),
        "fix_hypothesis": rp["diagnosis"]["fix_hypothesis"],
        "confidence": 0.9})
    must(sid, "notary_contract.freeze", {
        "assertions": rp["contract_assertions"], "in_scope": rp["scope"],
        "out_of_scope": rp.get("out_of_scope", [])})


def ref_impl(sid: str) -> dict:
    fixture = json.loads(
        (PKG_ROOT / "scenarios" / f"{sid}.json").read_text(encoding="utf-8"))
    return {"queue_box.py": fixture["reference"]["implementation"]}


def ref_tests(sid: str) -> dict:
    fixture = json.loads(
        (PKG_ROOT / "scenarios" / f"{sid}.json").read_text(encoding="utf-8"))
    return {"test_blind_contract.py": fixture["reference"]["tests"]}


def buggy_target(sid: str) -> dict:
    """The ORIGINAL (pre-fix) target source — reproduces the planted bug."""
    fixture = json.loads(
        (PKG_ROOT / "scenarios" / f"{sid}.json").read_text(encoding="utf-8"))
    return {name: (PKG_ROOT / "tools" / "notary_target" / name
                   ).read_text(encoding="utf-8")
            for name in fixture["target_files"]}


def drive_red(sid: str, first: bool = False) -> None:
    """Drive to REJECTED via the original buggy implementation (test red)."""
    if first:
        replay_to_contract(sid)
    must(sid, "notary_author.submit_implementation",
         {"files": buggy_target(sid)})
    if first:
        must(sid, "notary_tester.submit_tests", {"files": ref_tests(sid)})
    tg = must(sid, "notary_gate.run_test_gate")
    if tg["decision"] != "red":
        raise SystemExit(f"setup assumption broken: expected red, got {tg}")


VACUOUS_TESTS = '''import unittest


class TestVacuous(unittest.TestCase):
    def test_trivially_true(self):
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
'''


def artifact_case(case_id: str, impl_files: dict, test_files: dict,
                  expect: str, note: str) -> None:
    sid = "qb_inhouse_fix"
    replay_to_contract(sid)
    must(sid, "notary_author.submit_implementation", {"files": impl_files})
    must(sid, "notary_tester.submit_tests", {"files": test_files})
    tg = must(sid, "notary_gate.run_test_gate")
    if expect == "test_red":
        ok = tg["decision"] == "red"
        detail = f"test gate: {tg['decision']} — {tg['summary']}"
        if ok:
            state = must(sid, "notary_state.get")["state"]
            ok = state == "REJECTED"
            detail += f" | final: {state}"
    elif expect == "mutation_red":
        if tg["decision"] != "green":
            record(case_id, "adversarial-artifact", expect, False,
                   f"setup assumption broken: test gate {tg['decision']}")
            return
        mg = must(sid, "notary_gate.run_mutation_gate")
        if mg.get("status", "").startswith("awaiting_rebuttal"):
            final = must(sid, "notary_gate.finalize_mutation")
            decision = final["verdict"]["decision"]
            summary = final["verdict"]["summary"]
        else:
            decision = mg.get("verdict", {}).get("decision")
            summary = str(mg.get("summary"))
        ok = decision == "red"
        detail = f"mutation gate: {decision} — {summary}"
        if ok:
            state = must(sid, "notary_state.get")["state"]
            ok = state == "REJECTED"
            detail += f" | final: {state}"
    else:
        raise ValueError(expect)
    record(case_id, "adversarial-artifact", expect, ok and gateway_alive(),
           detail + f" | {note}")


def main() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--host", "127.0.0.1", "--port",
         str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"{BASE}/health", timeout=1):
                    break
            except Exception:
                time.sleep(0.2)

        S = "qb_inhouse_fix"
        must(S, "reset")

        # --- A. unknown targets ---------------------------------------
        expect_error("A1-unknown-scenario", "routing",
                     "no_such_scenario", "notary_state.get")
        expect_error("A2-unknown-scenario-scan", "routing",
                     "no_such_scenario", "notary_sentinel.scan")
        expect_error("A3-unknown-tool", "routing", S, "notary_bogus.tool")

        # --- B. malformed payloads ------------------------------------
        expect_error("B1-truncated-json", "malformed-payload", S,
                     "notary_state.get", raw_body=b'{"x": ')
        expect_error("B2-triage-invalid-verdict", "malformed-payload", S,
                     "notary_flow.triage", {"verdict": "maybe"})
        expect_error("B3-triage-empty-verdict", "malformed-payload", S,
                     "notary_flow.triage", {"verdict": "  "})
        expect_error("B4-skill-unsafe-name", "malformed-payload", S,
                     "notary_skill.register",
                     {"name": "../evil", "content": "x"})
        expect_error("B5-skill-empty-content", "malformed-payload", S,
                     "notary_skill.register", {"name": "ok-name", "content": ""})

        # --- C. illegal state transitions (fresh scenario) ------------
        must(S, "reset")
        expect_error("C1-deploy-before-notarized", "illegal-transition", S,
                     "notary_release.deploy", {"version": "v9.9.9"})
        expect_error("C2-rebuttal-before-mutation", "illegal-transition", S,
                     "notary_rebuttal.submit",
                     {"mutant_id": "M01", "kind": "equivalent_mutant",
                      "justification": "x"})
        expect_error("C3-finalize-before-mutation", "illegal-transition", S,
                     "notary_gate.finalize_mutation")
        expect_error("C4-resolve-human-not-escalated", "illegal-transition",
                     S, "notary_flow.resolve_human", {"approve": True})
        expect_error("C5-external-change-on-inhouse", "illegal-transition",
                     S, "notary_change.get_submitted_change")

        # --- E. role policy (least-privilege whitelist) ---------------
        replay_to_contract(S)
        se_path = PKG_ROOT / "runs" / S / "evidence" / "security_events.json"

        # E1: author may not read the tester blind partition — refusal
        # must be explicit AND persisted as a security event.
        ok, body = raw_call(S, "notary_tester.get_context", {"role": "author"})
        events = json.loads(se_path.read_text()) if se_path.exists() else []
        good = (not ok and bool(body.get("error"))
                and any(e.get("event") == "role-denied"
                        and e.get("role") == "author" for e in events)
                and gateway_alive())
        record("E1-author-reads-tester-partition", "role-policy",
               "explicit refusal + persisted security event", good,
               str(body.get("error", body))[:90])

        # E2: tester may not submit the implementation (blind partition
        # holds in both directions).
        ok, body = raw_call(S, "notary_author.submit_implementation",
                            {"role": "tester", "files": ref_impl(S)})
        record("E2-tester-submits-implementation", "role-policy",
               "explicit refusal", not ok and bool(body.get("error"))
               and gateway_alive(), str(body.get("error", body))[:90])

        # E3: author may not register skills (postmortem-only endpoint).
        ok, body = raw_call(S, "notary_skill.register",
                            {"role": "author", "name": "fuzz-sneak",
                             "content": "smuggled skill"})
        record("E3-author-registers-skill", "role-policy",
               "explicit refusal", not ok and bool(body.get("error"))
               and gateway_alive(), str(body.get("error", body))[:90])

        # E4: unknown (made-up) roles get no privileges on restricted
        # endpoints — the whitelist is not fooled by self-declaration.
        ok, body = raw_call(S, "notary_contract.freeze",
                            {"role": "ceo", "assertions": ["long enough assertion"]})
        record("E4-self-declared-role", "role-policy",
               "explicit refusal", not ok and bool(body.get("error"))
               and gateway_alive(), str(body.get("error", body))[:90])

        # E5: calls without a role still proceed (backward compatible) but
        # are traced as "unknown" — unaudited identity is always visible.
        ok, body = raw_call(S, "notary_author.get_context")
        trace_lines = (PKG_ROOT / "runs" / S / "trace.jsonl"
                       ).read_text().strip().splitlines()
        last = json.loads(trace_lines[-1])
        good = (ok and last.get("role") == "unknown" and gateway_alive())
        record("E5-roleless-call-traced-unknown", "role-policy",
               "allowed + trace marks role 'unknown'", good,
               f"ok={ok} trace role={last.get('role')!r}")

        # E6: the correct role passes the same endpoint — the policy
        # denies wrong roles, not legitimate work.
        ok, body = raw_call(S, "notary_author.get_context", {"role": "author"})
        record("E6-correct-role-passes", "role-policy",
               "allowed", ok and gateway_alive(),
               f"ok={ok}")

        # --- F. skill runtime security --------------------------------
        # F1: an incompatible skill (declares a future gateway) must be
        # rejected at load AND refused when fetched — never served.
        rogue = PKG_ROOT / "skills" / "registry" / "fuzz-rogue-skill.md"
        try:
            rogue.write_text(
                "---\nname: fuzz-rogue-skill\ndescription: from the future\n"
                "version: 9.9.9\ncompat: gateway>=99.0\n---\n\nrogue\n",
                encoding="utf-8")
            ok_list, body_list = raw_call(S, "notary_skill.list")
            rec = next((s for s in body_list["result"]["skills"]
                        if s["name"] == "fuzz-rogue-skill"), None)
            ok_get, body_get = raw_call(S, "notary_skill.get",
                                        {"name": "fuzz-rogue-skill"})
            good = (ok_list and rec is not None
                    and rec["status"] == "rejected"
                    and not ok_get and bool(body_get.get("error"))
                    and gateway_alive())
            record("F1-incompatible-skill-refused", "skill-security",
                   "rejected at load + refused on get", good,
                   f"load status={rec and rec['status']!r} "
                   f"get error={str(body_get.get('error'))[:60]}")
        finally:
            rogue.unlink(missing_ok=True)

        # F2: unknown trigger signals are explicit errors listing the
        # valid signals — no fuzzy fallback matching.
        expect_error("F2-match-unknown-signal", "skill-security", S,
                     "notary_skill.match", {"signal": "make-it-up"})

        # F3: fetching a nonexistent skill is an explicit error.
        expect_error("F3-get-unknown-skill", "skill-security", S,
                     "notary_skill.get", {"name": "no-such-skill"})

        # F4: match accuracy spot check — the injection signal must
        # resolve to input-validation-injection, loaded and serveable.
        ok, body = raw_call(S, "notary_skill.match",
                            {"signal": "external-input-handling"})
        res = body.get("result", {})
        good = (ok and res.get("skill") == "input-validation-injection"
                and res.get("status") == "matched" and gateway_alive())
        record("F4-match-accuracy-spot", "skill-security",
               "signal resolves to declared skill", good,
               f"matched {res.get('skill')!r} status {res.get('status')!r}")

        # --- G. entry conditions & loop guards -------------------------
        # G1: implementation before the contract exists — blind partitions
        # are meaningless without a frozen contract.
        must(S, "reset")
        expect_error("G1-submit-impl-before-contract", "entry-guard", S,
                     "notary_author.submit_implementation",
                     {"files": ref_impl(S)})
        # G2: blind tests before the contract — same hole, other side.
        expect_error("G2-submit-tests-before-contract", "entry-guard", S,
                     "notary_tester.submit_tests",
                     {"files": ref_tests(S)})
        # G3: gates cannot be invoked before the adversarial loop.
        expect_error("G3-test-gate-before-authoring", "entry-guard", S,
                     "notary_gate.run_test_gate")
        # G4: rework only from REJECTED.
        expect_error("G4-rework-not-rejected", "illegal-transition", S,
                     "notary_flow.request_rework",
                     {"role": "leader", "reason": "too early"})
        # G5: rework budget is real — two reworks allowed, third refused.
        drive_red(S, first=True)
        for rnd in (1, 2):
            ok, body = raw_call(S, "notary_flow.request_rework",
                                {"role": "leader",
                                 "reason": f"rework round {rnd}"})
            if not ok:
                record("G5-rework-budget-exhausted", "loop-guard",
                       "third rework refused", False,
                       f"round {rnd} unexpectedly refused: {body}")
                break
            drive_red(S)
        else:
            ok, body = raw_call(S, "notary_flow.request_rework",
                                {"role": "leader", "reason": "third rework"})
            record("G5-rework-budget-exhausted", "loop-guard",
                   "explicit refusal naming the budget",
                   not ok and "budget exhausted" in str(body.get("error", ""))
                   and gateway_alive(),
                   str(body.get("error", body))[:90])
        # G6: no escalation from a terminal state.
        expect_error("G6-escalate-from-rejected", "illegal-transition", S,
                     "notary_flow.triage",
                     {"verdict": "escalate", "scope": ["queue_box.py"]})
        # G7: the contract cannot be re-frozen without an escalation.
        replay_to_contract(S)
        expect_error("G7-double-freeze", "illegal-transition", S,
                     "notary_contract.freeze",
                     {"assertions": ["a second freeze must fail here"]})

        # --- H. skill lifecycle (index integrity / chain / rollback) ----
        import shutil as _shutil
        reg = PKG_ROOT / "skills" / "registry"
        bak = PKG_ROOT / "skills" / "registry.fuzzbak"
        if bak.exists():
            _shutil.rmtree(bak)
        _shutil.copytree(reg, bak)
        try:
            # H0 setup: register a probe so a registry file is guaranteed
            # to exist regardless of what ran before this suite.
            must(S, "notary_skill.register",
                 {"role": "postmortem", "name": "fuzz-lifecycle-probe",
                  "content": "# probe (fuzz)"})
            # H1: a tampered registry file is rejected at load AND refused
            # on get — runtime integrity is enforced, not narrated.
            victim = reg / "fuzz-lifecycle-probe.md"
            original = victim.read_bytes()
            victim.write_bytes(original + b"\n# tampered\n")
            ok, body = raw_call(S, "notary_skill.list")
            rec = next((s for s in body["result"]["skills"]
                        if s["name"] == "fuzz-lifecycle-probe"), None)
            ok_get, body_get = raw_call(
                S, "notary_skill.get", {"name": "fuzz-lifecycle-probe"})
            good = (ok and rec and rec["status"] == "rejected"
                    and not ok_get and gateway_alive())
            record("H1-tampered-skill-refused", "skill-lifecycle",
                   "rejected at load + refused on get", good,
                   f"status={rec and rec['status']!r} "
                   f"reason={rec and str(rec.get('reason'))[:50]!r}")
            victim.write_bytes(original)

            # H2: supersedes chain — v2 of a SEED is served when present;
            # retiring v2 rolls back to the seed (append-only tombstone).
            must(S, "notary_skill.register",
                 {"role": "postmortem", "name": "boundary-condition-check-v2",
                  "version": "2.0.0",
                  "supersedes": "boundary-condition-check",
                  "content": "# v2 (fuzz)"})
            r1 = must(S, "notary_skill.match",
                      {"signal": "enumerate-boundaries"})
            must(S, "notary_skill.retire",
                 {"role": "leader", "name": "boundary-condition-check-v2",
                  "reason": "fuzz: rollback drill"})
            r2 = must(S, "notary_skill.match",
                      {"signal": "enumerate-boundaries"})
            good = (r1["skill"] == "boundary-condition-check-v2"
                    and r1.get("resolved_from") == "boundary-condition-check"
                    and r2["skill"] == "boundary-condition-check"
                    and gateway_alive())
            record("H2-version-chain-rollback", "skill-lifecycle",
                   "v2 served; retire v2 -> v1 serves", good,
                   f"before={r1['skill']} after={r2['skill']}")

            # H3: retired skill refused on get; a file with no index entry
            # (smuggled into the registry dir) is rejected at load.
            ok_get, body_get = raw_call(
                S, "notary_skill.get", {"name": "boundary-condition-check-v2"})
            (reg / "smuggled.md").write_text(
                "---\nname: smuggled\n---\n\nno index entry\n",
                encoding="utf-8")
            ok_l, body_l = raw_call(S, "notary_skill.list")
            smug = next((s for s in body_l["result"]["skills"]
                         if s["name"] == "smuggled"), None)
            good = (not ok_get and smug is not None
                    and smug["status"] == "rejected" and gateway_alive())
            record("H3-retired-and-unindexed-refused", "skill-lifecycle",
                   "retired get refused; unindexed rejected", good,
                   f"get_ok={ok_get} smuggled={smug and smug['status']!r}")
        finally:
            _shutil.rmtree(reg)
            _shutil.move(str(bak), str(reg))

        # --- I. intake endpoint (CI/webhook integration) ----------------
        # I1: a well-formed intake mints a scenario, is idempotent on
        # retry, and the run is immediately drivable.
        intake_payload = {"role": "ci", "target": "queue_box",
                          "title": "intake fuzz: pop boundary",
                          "report": "CI webhook report: pop() behavior at "
                                    "empty mailbox needs notarization",
                          "expected_behavior": "pop on empty raises a clean "
                                               "contract error"}
        ok, body = raw_call(S, "notary_intake.submit_issue", intake_payload)
        sid_new = body.get("result", {}).get("scenario_id")
        ok2, body2 = raw_call(S, "notary_intake.submit_issue", intake_payload)
        drivable = False
        if sid_new:
            ok3, body3 = raw_call(sid_new, "notary_change.get_issue")
            drivable = ok3 and body3["result"]["issue"]["title"].startswith(
                "intake fuzz")
        good = (ok and sid_new and not ok2  # retry refused as duplicate
                and "already intaken" in str(body2.get("error", ""))
                and drivable and gateway_alive())
        record("I1-intake-mint-and-idempotent", "intake",
               "mints scenario, idempotent, drivable", good,
               f"sid={sid_new} retry_error={str(body2.get('error'))[:40]!r}")
        # cleanup the minted scenario + run dir
        if sid_new:
            (PKG_ROOT / "scenarios" / f"{sid_new}.json").unlink(
                missing_ok=True)
            import shutil as _sh
            _sh.rmtree(PKG_ROOT / "runs" / sid_new, ignore_errors=True)

        # I2: under-specified intake (no auditable report) refused.
        expect_error("I2-intake-too-thin", "intake", S,
                     "notary_intake.submit_issue",
                     {"role": "ci", "target": "queue_box", "title": "x",
                      "report": "short", "expected_behavior": "short"})
        # I3: intake by a business role refused (CI/leader/triage only).
        expect_error("I3-intake-wrong-role", "intake", S,
                     "notary_intake.submit_issue",
                     {"role": "author", **{k: v for k, v in
                                           intake_payload.items()
                                           if k != "role"}})

        # --- J. policy config & upload intake ---------------------------
        # J1: an invalid notary.json must refuse startup (fail-closed on
        # policy misconfiguration — a misconfigured notary is worse than
        # a stopped one).
        import tempfile as _tmpf
        bad_cfg = Path(_tmpf.mkdtemp()) / "notary.json"
        bad_cfg.write_text('{"mutation": {"red_below": "high"}}')
        proc_bad = subprocess.Popen(
            [sys.executable, str(GATEWAY), "--host", "127.0.0.1",
             "--port", "18094", "--config", str(bad_cfg)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out_bad, _ = proc_bad.communicate(timeout=15)
        good = proc_bad.returncode != 0 and "must be" in out_bad
        record("J1-invalid-config-refused", "policy-config",
               "startup refused with typed error", good,
               out_bad.strip().splitlines()[-1][:80] if out_bad else "")
        bad_cfg.unlink(missing_ok=True)

        # J2: upload intake (external mode) — a user-submitted patch is
        # minted into a drivable external scenario and quarantined/scanned
        # like any submission.
        upload = {"role": "ci", "target": "queue_box",
                  "title": "upload fuzz: patch with shell call",
                  "report": "user-uploaded patch claiming to fix empty pop",
                  "expected_behavior": "pop on empty raises a clean contract error",
                  "files": {"queue_box.py": "import os\nos.system('echo hi')\n"
                                            "class Mailbox:\n    pass\n"}}
        ok, body = raw_call(S, "notary_intake.submit_issue", upload)
        sid_up = body.get("result", {}).get("scenario_id")
        mode_ok = body.get("result", {}).get("mode") == "external"
        scanned = False
        if sid_up:
            ok_s, body_s = raw_call(sid_up, "notary_sentinel.scan")
            scanned = ok_s and body_s["result"]["decision"] == "pass"
        good = (ok and mode_ok and scanned and gateway_alive())
        record("J2-upload-intake-external", "intake",
               "external scenario minted + scanned", good,
               f"sid={sid_up} mode={body.get('result', {}).get('mode')!r}")
        if sid_up:
            (PKG_ROOT / "scenarios" / f"{sid_up}.json").unlink(missing_ok=True)
            import shutil as _sh2
            _sh2.rmtree(PKG_ROOT / "runs" / sid_up, ignore_errors=True)

        # J3–J6: custom-target upload ("自带代码送修") — upload validation
        # is fail-closed at intake; customer code only ever executes inside
        # the rlimit sandbox AFTER quarantine passes.
        custom_base = {"role": "ci",
                       "title": "calc module needs a subtraction helper",
                       "report": "users asked for sub(a,b) alongside add; "
                                 "negative inputs must also work",
                       "expected_behavior": "1) add sub(a,b) returning a-b; "
                                            "2) add stays unchanged; "
                                            "3) only calc.py may change",
                       "source_files": {"calc.py": "def add(a, b):\n"
                                                   "    return a + b\n"},
                       "test_files": {"test_calc.py":
                                      "import unittest\n"
                                      "from calc import add\n"
                                      "class T(unittest.TestCase):\n"
                                      "    def test_add(self):\n"
                                      "        self.assertEqual(add(1, 2), 3)\n"}}
        # J3: valid upload mints an inhouse custom-target scenario whose
        # embedded source is served back to the pipeline.
        ok3, body3 = raw_call(S, "notary_intake.submit_issue", custom_base)
        sid_c = body3.get("result", {}).get("scenario_id")
        src_back = False
        if sid_c:
            ok_s3, body_s3 = raw_call(sid_c, "notary_repo.get_source")
            src_back = ok_s3 and "def add" in str(
                body_s3.get("result", {}).get("files", {}).get("calc.py", ""))
        good = (ok3 and sid_c
                and body3.get("result", {}).get("mode") == "inhouse"
                and src_back and gateway_alive())
        record("J3-custom-upload-minted", "intake",
               "custom target minted + embedded source served", good,
               f"sid={sid_c} mode={body3.get('result', {}).get('mode')!r}")
        if sid_c:
            (PKG_ROOT / "scenarios" / f"{sid_c}.json").unlink(missing_ok=True)
            import shutil as _sh3
            _sh3.rmtree(PKG_ROOT / "runs" / sid_c, ignore_errors=True)

        # J4: no tests uploaded -> refused (first notary principle: no
        # notarization without tests, however small they are).
        expect_error("J4-custom-no-tests-refused", "intake", S,
                     "notary_intake.submit_issue",
                     {k: v for k, v in custom_base.items()
                      if k != "test_files"})
        # J5: executable-dangerous code in upload -> strict quarantine
        # refuses BEFORE any scenario exists (os.system is reject-on-sight
        # for customer code that would run in the sealed executor).
        expect_error("J5-custom-quarantine-refused", "intake", S,
                     "notary_intake.submit_issue",
                     {**custom_base,
                      "source_files": {"evil.py": "import os\n"
                                                  "os.system('id')\n"}})
        # J6: patch (files) and own-code (source_files) together ->
        # mutually exclusive, refused.
        expect_error("J6-custom-mixed-mode-refused", "intake", S,
                     "notary_intake.submit_issue",
                     {**custom_base, "files": {"a.py": "x = 1\n"}})

        # J7: advisory mode (2b) — customer uploaded no tests but asked
        # for advice: scenario mints with advisory flag and empty baseline,
        # instead of being refused.
        ok7, body7 = raw_call(S, "notary_intake.submit_issue",
                              {k: v for k, v in {**custom_base,
                                                 "advisory": True}.items()
                               if k != "test_files"})
        sid_a = body7.get("result", {}).get("scenario_id")
        fx7 = None
        if sid_a:
            fx7 = json.loads((PKG_ROOT / "scenarios" / f"{sid_a}.json")
                             .read_text(encoding="utf-8"))
        good = (ok7 and sid_a and fx7 and fx7.get("advisory") is True
                and fx7.get("baseline_test_files") == []
                and gateway_alive())
        record("J7-advisory-no-tests-minted", "intake",
               "advisory scenario minted (flag + empty baseline)", good,
               f"sid={sid_a}")
        if sid_a:
            (PKG_ROOT / "scenarios" / f"{sid_a}.json").unlink(missing_ok=True)
            import shutil as _sh7
            _sh7.rmtree(PKG_ROOT / "runs" / sid_a, ignore_errors=True)

        # J8: certificate predicate — advisory fixtures must NOT get a
        # certificate at seal time; normal fixtures must (unit check).
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("gw_for_pred", GATEWAY)
        _gw = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_gw)
        good = (_gw.should_issue_certificate({"advisory": True}) is False
                and _gw.should_issue_certificate({}) is True
                and _gw.should_issue_certificate({"advisory": False}) is True)
        record("J8-advisory-no-certificate", "intake",
               "seal skips certificate for advisory only", good,
               "should_issue_certificate predicate")

        # --- D. adversarial artifacts ---------------------------------
        artifact_case("D1-empty-implementation", {"queue_box.py": ""},
                      ref_tests(S), "test_red",
                      "empty module must never pass the test gate")
        artifact_case("D2-syntax-error-implementation",
                      {"queue_box.py": "def broken(:\n"}, ref_tests(S),
                      "test_red", "unimportable code must never pass")
        artifact_case("D3-vacuous-blind-tests", ref_impl(S),
                      {"test_blind_contract.py": VACUOUS_TESTS},
                      "mutation_red",
                      "always-true tests kill too few mutants -> red")
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    total = len(results)
    closed = sum(1 for r in results if r["verdict"] == "fail-closed")
    summary = {"cases_total": total, "fail_closed": closed,
               "fail_closed_rate": f"{closed}/{total}",
               "silent_pass": total - closed}
    out = {"summary": summary, "cases": results}
    (EVALSET / "fuzz_results.json").write_text(
        json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    print(f"\n===== fuzz summary: {closed}/{total} fail-closed =====")
    print(f"results -> {EVALSET / 'fuzz_results.json'}")
    if closed != total:
        sys.exit(1)
    print("GATEWAY IS FAIL-CLOSED ON ALL FUZZ CASES")


if __name__ == "__main__":
    main()
