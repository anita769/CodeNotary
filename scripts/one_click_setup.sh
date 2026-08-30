#!/usr/bin/env bash
# CodeNotary one-click setup & self-check.
#
# Target audience: a reviewer (or an ops engineer) on a clean machine.
# One command brings up the gateway + console and proves the system works:
#
#   ./scripts/one_click_setup.sh           # full setup + smoke + report
#   ./scripts/one_click_setup.sh --stop    # stop gateway & console
#
# Exit codes: 0 ok | 1 env missing | 2 port busy | 3 gateway failed |
#             4 smoke failed | 5 console failed
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GW_PORT="${GW_PORT:-18090}"
CONSOLE_PORT="${CONSOLE_PORT:-18091}"
RUN_DIR="$ROOT/.run"
mkdir -p "$RUN_DIR"

say()  { printf '\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*" >&2; exit "$1"; }

if [ "${1:-}" = "--stop" ]; then
    say "stopping"
    [ -f "$RUN_DIR/gateway.pid" ] && kill "$(cat "$RUN_DIR/gateway.pid")" 2>/dev/null && rm -f "$RUN_DIR/gateway.pid"
    [ -f "$RUN_DIR/console.pid" ] && kill "$(cat "$RUN_DIR/console.pid")" 2>/dev/null && rm -f "$RUN_DIR/console.pid"
    echo "stopped"
    exit 0
fi

say "1/6 environment check"
command -v python3 >/dev/null || fail 1 "python3 not found"
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail 1 "python3 >= 3.9 required (found $PYVER)"
echo "python3 $PYVER OK (stdlib only — no pip install needed)"

say "2/6 port check"
for port in "$GW_PORT" "$CONSOLE_PORT"; do
    if curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$port/health" \
       || curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$port/"; then
        fail 2 "port $port already in use (run with --stop first, or override GW_PORT/CONSOLE_PORT)"
    fi
done
echo "ports $GW_PORT / $CONSOLE_PORT free"

say "3/6 starting notary gateway"
nohup python3 "$ROOT/tools/notary_gateway.py" --host 0.0.0.0 --port "$GW_PORT" \
    > "$RUN_DIR/gateway.log" 2>&1 &
echo $! > "$RUN_DIR/gateway.pid"
for _ in $(seq 30); do
    curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$GW_PORT/health" && break
    sleep 0.3
done
curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$GW_PORT/health" \
    || fail 3 "gateway did not come up; see $RUN_DIR/gateway.log"
echo "gateway up: http://127.0.0.1:$GW_PORT  (metrics: /metrics)"

say "4/6 smoke: local dryrun (live gates, no LLM)"
if python3 "$ROOT/scripts/local_dryrun.py" > "$RUN_DIR/dryrun.log" 2>&1; then
    echo "dryrun OK (log: $RUN_DIR/dryrun.log)"
else
    fail 4 "dryrun failed; see $RUN_DIR/dryrun.log"
fi

say "5/6 starting console"
nohup python3 "$ROOT/tools/notary_console.py" --port "$CONSOLE_PORT" \
    > "$RUN_DIR/console.log" 2>&1 &
echo $! > "$RUN_DIR/console.pid"
sleep 1
curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$CONSOLE_PORT/" \
    || fail 5 "console did not come up; see $RUN_DIR/console.log"
echo "console up: http://127.0.0.1:$CONSOLE_PORT"

say "6/6 acceptance report"
cat <<EOF
CodeNotary is up.

  gateway : http://127.0.0.1:$GW_PORT  (health /health, metrics /metrics)
  console : http://127.0.0.1:$CONSOLE_PORT
  logs    : $RUN_DIR/

Reproduce every evaluation number (deterministic, no network, no LLM):
  python3 scripts/eval_replay.py          # scenario replay (expect TOTAL 35/35)
  python3 scripts/eval_skill_coverage.py  # skill runtime audit (12 signals)
  python3 scripts/eval_fuzz.py            # fail-closed suite (expect 39/39)
  python3 scripts/eval_checkpoint.py      # crash recovery (SIGKILL resume)
  python3 scripts/eval_report.py          # regenerate EVALUATION.md

Stop:  $0 --stop
EOF
