"""Capacity probe: measure the operational cost of one notarization run.

Answers the capacity questions with measured numbers, not estimates:
  - end-to-end wall time per run (aggregated from trace metrics)
  - per-gate execution time (test / mutation / convention)
  - mutation gate cost model (mutants x test-suite runs)
  - gateway peak RSS while driving the heaviest scenario
  - evidence-package disk footprint per run

Output: evalset/capacity.json — OPERATIONAL data, measured on this machine
and explicitly non-deterministic (unlike results.json): re-run to re-measure.
EVALUATION.md deliberately does not embed these numbers; the tech report
quotes them with an environment label (§19).

Usage:  python3 scripts/capacity_probe.py
"""

from __future__ import annotations

import json
import platform
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
GATEWAY = PKG_ROOT / "tools" / "notary_gateway.py"
EVALSET = PKG_ROOT / "evalset"
RUNS = PKG_ROOT / "runs"
PORT = 18094
BASE = f"http://127.0.0.1:{PORT}"


def call(sid: str, tool: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}/tools/{sid}/{tool}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))["result"]


def peak_rss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return 0.0


def dir_size_kb(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) // 1024


def main() -> None:
    # 1. aggregate per-run timing from metrics.json (written by replay)
    walls, gate_ms, mutations = [], {}, []
    for mpath in sorted(RUNS.glob("*/metrics.json")):
        m = json.loads(mpath.read_text())
        if m.get("wall_time_s"):
            walls.append(m["wall_time_s"])
        for gate, ms in m.get("gate_duration_ms", {}).items():
            gate_ms.setdefault(gate, []).append(ms)
    # mutation cost model from one sample run's verdict
    for vpath in sorted(RUNS.glob("*/verdicts/mutation.json")):
        v = json.loads(vpath.read_text())
        if "mutants_total" in str(v):
            pass  # verdict summaries don't carry counts; trace has them

    # 2. peak RSS while driving the heaviest scenario (1000-line target,
    #    12 mutants x full unittest each)
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--host", "127.0.0.1", "--port",
         str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    heavy = {}
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"{BASE}/health", timeout=1):
                    break
            except Exception:
                time.sleep(0.2)
        base_rss = peak_rss_mb(proc.pid)
        t0 = time.monotonic()
        manifest = json.loads(
            (EVALSET / "manifest.json").read_text(encoding="utf-8"))
        sample = next(s for s in manifest["samples"]
                      if s["id"] == "d5-pkg_epoch_prefix")
        sid = sample["scenario"]
        rp = sample["replay"]
        fixture = json.loads(
            (PKG_ROOT / "scenarios" / f"{sid}.json").read_text(
                encoding="utf-8"))
        call(sid, "reset")
        call(sid, "notary_change.get_issue")
        call(sid, "notary_sentinel.scan")
        call(sid, "notary_flow.triage", {"verdict": "accept",
                                         "scope": rp["scope"]})
        call(sid, "notary_flow.diagnosis", {
            "root_cause": rp["diagnosis"]["root_cause"], "evidence": ["probe"],
            "fix_hypothesis": rp["diagnosis"]["fix_hypothesis"],
            "confidence": 0.9})
        call(sid, "notary_contract.freeze", {
            "assertions": rp["contract_assertions"],
            "in_scope": rp["scope"]})
        call(sid, "notary_author.submit_implementation", {
            "files": {"d5b_specifiers.py":
                      fixture["reference"]["implementation"]}})
        call(sid, "notary_tester.submit_tests", {
            "files": {"test_blind_contract.py": fixture["reference"]["tests"]}})
        call(sid, "notary_gate.run_test_gate")
        mg = call(sid, "notary_gate.run_mutation_gate")
        if mg.get("status", "").startswith("awaiting_rebuttal"):
            for s in call(sid, "notary_gate.get_survivors")["survivors"]:
                call(sid, "notary_rebuttal.submit", {
                    "mutant_id": s["id"], "kind": "equivalent_mutant",
                    "justification": "probe"})
            mg = call(sid, "notary_gate.finalize_mutation")
        call(sid, "notary_gate.run_convention_gate")
        call(sid, "notary_evidence.seal")
        heavy = {
            "scenario": sid,
            "wall_s": round(time.monotonic() - t0, 2),
            "gateway_rss_baseline_mb": base_rss,
            "gateway_rss_peak_mb": peak_rss_mb(proc.pid),
            "mutants_evaluated": mg.get("mutants_total") or
                mg.get("verdict", {}).get("mutants_total"),
            "run_dir_kb": dir_size_kb(RUNS / sid),
        }
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    out = {
        "note": "operational measurement, non-deterministic; re-run to "
                "re-measure. Not part of the deterministic evaluation.",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
        },
        "runs_aggregated": len(walls),
        "wall_time_s": {
            "median": round(statistics.median(walls), 2) if walls else 0,
            "max": round(max(walls), 2) if walls else 0,
        },
        "gate_duration_ms_median": {
            g: round(statistics.median(v), 1)
            for g, v in sorted(gate_ms.items())},
        "heaviest_run": heavy,
    }
    (EVALSET / "capacity.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
