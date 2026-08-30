"""Alert watchdog for the CodeNotary pipeline (operations layer).

Polls runs/ and emits operational alerts to alerts.log. Four rules:

  stall      — a non-terminal run whose trace has been silent longer than
               --stall-seconds (worker asleep, leader stuck, pacemaker owed)
  escalated  — a run sitting in ESCALATED (human arbitration owed)
  rejected   — a run that reached REJECTED (terminal, needs triage/rework
               decision)
  security   — role-policy denials present in evidence/security_events.json

Design note (see tech report §18/§19): timeouts live HERE, in the platform
layer, never in the gateway — the gateway's determinism axiom forbids
wall-clock dependence. This watchdog is the活性 counterpart: the gateway
guarantees "whenever woken, the state is legal"; the watchdog guarantees
"someone gets woken".

Usage:
  python3 scripts/alert_watchdog.py --once            # single scan
  python3 scripts/alert_watchdog.py --interval 60     # loop forever
  python3 scripts/alert_watchdog.py --once --runs-dir /path --alerts-log /path
Exit: 0 always for loop mode; --once exits 1 if any alert fired (CI-usable).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {"RELEASED", "REJECTED", "ROLLED_BACK", "QUARANTINED"}


def scan(runs_dir: Path, stall_seconds: float, now: float) -> list[dict]:
    alerts: list[dict] = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        sid = run_dir.name
        cp = run_dir / "checkpoint.json"
        trace = run_dir / "trace.jsonl"
        if not cp.exists() or not trace.exists():
            continue
        try:
            state = json.loads(cp.read_text())["sm"]["state"]
            lines = [l for l in trace.read_text().splitlines() if l.strip()]
        except (OSError, json.JSONDecodeError, KeyError):
            continue
        if not lines:
            continue
        last_ts = json.loads(lines[-1])["ts"]

        if state not in TERMINAL and now - last_ts > stall_seconds:
            alerts.append({
                "rule": "stall", "run": sid, "state": state,
                "detail": f"trace silent for {now - last_ts:.0f}s "
                          f"(threshold {stall_seconds:.0f}s); "
                          f"worker/leader likely asleep"})
        if state == "ESCALATED":
            alerts.append({
                "rule": "escalated", "run": sid, "state": state,
                "detail": "awaiting human arbitration"})
        if state == "REJECTED":
            alerts.append({
                "rule": "rejected", "run": sid, "state": state,
                "detail": "terminal rejection; decide: request_rework "
                          "(budget permitting) / contract revision / archive"})
        se = run_dir / "evidence" / "security_events.json"
        if se.exists():
            try:
                events = json.loads(se.read_text())
            except (OSError, json.JSONDecodeError):
                events = []
            if events:
                alerts.append({
                    "rule": "security", "run": sid, "state": state,
                    "detail": f"{len(events)} role-policy denial(s); "
                              f"latest: {events[-1].get('detail', '')[:80]}"})
    return alerts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default=str(PKG_ROOT / "runs"))
    ap.add_argument("--alerts-log", default=str(PKG_ROOT / "alerts.log"))
    ap.add_argument("--stall-seconds", type=float, default=300.0)
    ap.add_argument("--interval", type=float, default=0.0,
                    help="poll interval seconds; 0 = single scan")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--webhook", default=None,
                    help="POST each alert as JSON to this URL "
                         "(generic webhook; Feishu/DingTalk bot URLs work)")
    args = ap.parse_args()

    runs_dir = Path(args.runs_dir)
    log_path = Path(args.alerts_log)

    def one_pass() -> list[dict]:
        now = time.time()
        alerts = scan(runs_dir, args.stall_seconds, now)
        if alerts:
            with log_path.open("a", encoding="utf-8") as fh:
                for a in alerts:
                    fh.write(json.dumps({"ts": now, **a},
                                        ensure_ascii=False) + "\n")
            if args.webhook:
                import urllib.request
                for a in alerts:
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            args.webhook,
                            data=json.dumps({"ts": now, **a}).encode(),
                            headers={"Content-Type": "application/json"},
                            method="POST"), timeout=5)
                    except Exception:
                        pass  # alert delivery must never kill the watchdog
        return alerts

    if args.once or args.interval <= 0:
        alerts = one_pass()
        for a in alerts:
            print(f"[{a['rule']}] {a['run']} ({a['state']}): {a['detail']}")
        print(f"{len(alerts)} alert(s); log -> {log_path}")
        sys.exit(1 if alerts else 0)

    print(f"watchdog polling {runs_dir} every {args.interval}s; "
          f"alerts -> {log_path}")
    while True:
        alerts = one_pass()
        for a in alerts:
            print(f"[{a['rule']}] {a['run']}: {a['detail']}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
