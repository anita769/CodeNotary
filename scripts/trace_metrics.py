"""Aggregate per-run collaboration metrics from notary gateway traces.

Reads runs/<scenario>/trace.jsonl (one JSON line per tool call) and emits
deterministic metrics.json — the collaboration-layer counterpart of the
gate-layer evaluation (eval_replay.py). Works on both replayed runs and
real AgentTeams runs; on real runs it answers the questions judges ask:

  - end-to-end wall time and per-stage latency
  - tool call count, gate execution time
  - adversarial-loop iterations (AUTHORING<->TESTING rework)
  - escalations and human interventions (resolve_human calls)

Usage:  python3 scripts/trace_metrics.py [runs_dir]
Writes: <runs_dir>/<scenario>/metrics.json for every run with a trace.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent

GATE_TOOLS = {
    "notary_sentinel.scan": "sentinel",
    "notary_gate.run_test_gate": "test",
    "notary_gate.run_mutation_gate": "mutation",
    "notary_gate.finalize_mutation": "mutation",
    "notary_gate.run_convention_gate": "convention",
}


def metrics_for(trace_path: Path) -> dict:
    events = [json.loads(line) for line in
              trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not events:
        return {"tool_calls": 0}

    tools = [e["tool"] for e in events]
    states = [e["state_after"] for e in events]
    ts0, ts1 = events[0]["ts"], events[-1]["ts"]

    gate_ms: dict[str, float] = {}
    for e in events:
        fam = GATE_TOOLS.get(e["tool"])
        if fam:
            gate_ms[fam] = round(gate_ms.get(fam, 0.0) + e["duration_ms"], 1)

    rework = sum(
        1 for prev, cur in zip(states, states[1:])
        if {prev, cur} == {"AUTHORING", "TESTING"} and prev != cur)

    # per-stage latency: seconds spent in each state (ts of next event - ts)
    stage_s: dict[str, float] = {}
    for e, nxt in zip(events, events[1:]):
        st = e["state_after"]
        stage_s[st] = round(stage_s.get(st, 0.0) + (nxt["ts"] - e["ts"]), 3)

    tool_counts: dict[str, int] = {}
    for t in tools:
        tool_counts[t] = tool_counts.get(t, 0) + 1

    # Role audit view: who called what. Traces written before gateway 0.6
    # carry no role field; they aggregate under "unknown" — which is itself
    # an honest audit signal (unattributed calls are visible, never hidden).
    role_counts: dict[str, int] = {}
    role_denied = 0
    for e in events:
        r = e.get("role", "unknown")
        role_counts[r] = role_counts.get(r, 0) + 1
        if e.get("event") == "role-denied":
            role_denied += 1
    skill_calls = {t: c for t, c in sorted(tool_counts.items())
                   if t.startswith("notary_skill.")}

    return {
        "run_id": events[0]["run_id"],
        "final_state": states[-1],
        "tool_calls": len(events),
        "distinct_tools": len(tool_counts),
        "tool_counts": dict(sorted(tool_counts.items())),
        "calls_by_role": dict(sorted(role_counts.items())),
        "role_denied_calls": role_denied,
        "skill_tool_calls": sum(skill_calls.values()),
        "skill_call_breakdown": skill_calls,
        "skill_match_calls": tool_counts.get("notary_skill.match", 0),
        "wall_time_s": round(ts1 - ts0, 3),
        "gate_duration_ms": gate_ms,
        "state_transitions": len(states) - 1,
        "adversarial_loop_iterations": rework,
        "escalations": states.count("ESCALATED"),
        "human_interventions": tools.count("notary_flow.resolve_human"),
        "rebuttals_submitted": tools.count("notary_rebuttal.submit"),
        "rework_requests": tools.count("notary_flow.request_rework"),
        "stage_latency_s": dict(sorted(stage_s.items())),
    }


def main() -> None:
    runs_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else PKG_ROOT / "runs"
    written = []
    for trace in sorted(runs_dir.glob("*/trace.jsonl")):
        m = metrics_for(trace)
        out = trace.parent / "metrics.json"
        out.write_text(json.dumps(m, ensure_ascii=False, sort_keys=True,
                                  indent=2) + "\n", encoding="utf-8")
        written.append(out)
        print(f"{m['run_id']:34s} final={m['final_state']:10s} "
              f"calls={m['tool_calls']:3d} wall={m['wall_time_s']:8.3f}s "
              f"rework={m['adversarial_loop_iterations']} "
              f"escalations={m['escalations']} "
              f"human={m['human_interventions']}")
    print(f"\n{len(written)} metrics.json written under {runs_dir}")


if __name__ == "__main__":
    main()
