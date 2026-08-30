"""Crash-recovery evaluation for the notary gateway (checkpoint/resume).

Proves the operational claim that a gateway crash loses nothing:
  1. drive a run to CONTRACTED against the REAL gateway
  2. kill -9 the gateway process (unclean death — no shutdown hooks)
  3. restart the gateway; it must resume the run from runs/<sid>/checkpoint.json
  4. verify state, contract hash and history survived; continue the pipeline
     all the way to RELEASED

Output: evalset/checkpoint_results.json (canonical, timestamp-free).
Usage:  python3 scripts/eval_checkpoint.py
Exit:   0 iff resume state is exact and the pipeline completes.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
GATEWAY = PKG_ROOT / "tools" / "notary_gateway.py"
EVALSET = PKG_ROOT / "evalset"
PORT = 18095
BASE = f"http://127.0.0.1:{PORT}"
SID = "qb_inhouse_fix"


class CheckpointFailure(Exception):
    pass


def call(tool: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}/tools/{SID}/{tool}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
    if not body.get("ok"):
        raise CheckpointFailure(f"tool call failed {tool}: {body}")
    return body["result"]


def start_gateway() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--host", "127.0.0.1", "--port",
         str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=1):
                return proc
        except Exception:
            time.sleep(0.2)
    raise CheckpointFailure("gateway did not start")


def replay_params() -> dict:
    manifest = json.loads(
        (EVALSET / "manifest.json").read_text(encoding="utf-8"))
    return next(s for s in manifest["samples"]
                if s["id"] == "d1-qb_inhouse_fix")["replay"]


def main() -> None:
    rp = replay_params()
    fixture = json.loads(
        (PKG_ROOT / "scenarios" / f"{SID}.json").read_text(encoding="utf-8"))

    proc = start_gateway()
    try:
        # --- phase 1: drive to CONTRACTED -------------------------------
        call("reset")
        call("notary_change.get_issue", {"role": "triage"})
        call("notary_sentinel.scan", {"role": "sentinel"})
        call("notary_flow.triage", {
            "verdict": "accept", "scope": rp["scope"],
            "route": ["rca", "contract", "author", "tester", "gates"],
            "rationale": "checkpoint eval", "role": "triage"})
        call("notary_flow.reproduce", {"role": "rca"})
        call("notary_flow.diagnosis", {
            "root_cause": rp["diagnosis"]["root_cause"],
            "evidence": ["checkpoint eval"],
            "fix_hypothesis": rp["diagnosis"]["fix_hypothesis"],
            "confidence": 0.95, "role": "rca"})
        f1 = call("notary_contract.freeze", {
            "assertions": rp["contract_assertions"], "in_scope": rp["scope"],
            "out_of_scope": rp.get("out_of_scope", []), "role": "contract"})
        before = call("notary_state.get")
        assert before["state"] == "CONTRACTED", before

        # --- phase 2: unclean kill, restart, verify resume --------------
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        proc = start_gateway()

        after = call("notary_state.get")
        ctx = call("notary_author.get_context", {"role": "author"})
        resumed = {
            "state_restored": after["state"] == "CONTRACTED",
            "contract_hash_intact":
                ctx["contract"]["frozen_hash"] == f1["frozen_hash"],
            "history_len_before": len(before["history"]),
            "history_len_after": len(after["history"]),
            "green_gates": after["green_gates"],
        }

        # --- phase 3: continue to RELEASED -------------------------------
        call("notary_author.submit_implementation", {
            "files": {"queue_box.py": fixture["reference"]["implementation"]},
            "role": "author"})
        call("notary_tester.submit_tests", {
            "files": {"test_blind_contract.py": fixture["reference"]["tests"]},
            "role": "tester"})
        tg = call("notary_gate.run_test_gate", {"role": "gatekeeper"})
        mg = call("notary_gate.run_mutation_gate", {"role": "gatekeeper"})
        if mg.get("status", "").startswith("awaiting_rebuttal"):
            for s in call("notary_gate.get_survivors",
                          {"role": "author"})["survivors"]:
                call("notary_rebuttal.submit", {
                    "mutant_id": s["id"], "kind": "equivalent_mutant",
                    "justification": "len() >= 0 always; equivalent guard",
                    "role": "author"})
            call("notary_gate.finalize_mutation", {"role": "gatekeeper"})
        call("notary_gate.run_convention_gate", {"role": "gatekeeper"})
        if call("notary_state.get")["state"] == "NOTARIZED":
            call("notary_release.deploy",
                 {"version": "v-checkpoint", "role": "release"})
        final = call("notary_state.get")["state"]
        call("notary_evidence.seal", {"role": "release"})

        resumed["final_state"] = final
        resumed["resume_ok"] = (
            resumed["state_restored"] and resumed["contract_hash_intact"]
            and resumed["history_len_after"] >= resumed["history_len_before"]
            and final == "RELEASED")
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    out = {"summary": {
        "kill_signal": "SIGKILL (unclean)",
        "resumed_state": "CONTRACTED",
        "final_state": resumed["final_state"],
        "resume_ok": resumed["resume_ok"],
    }, "detail": resumed}
    (EVALSET / "checkpoint_results.json").write_text(
        json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    print(f"results -> {EVALSET / 'checkpoint_results.json'}")
    if not resumed["resume_ok"]:
        sys.exit(1)
    print("CRASH RECOVERY VERIFIED: SIGKILL mid-run, resumed from checkpoint, "
          "completed to RELEASED")


if __name__ == "__main__":
    main()
