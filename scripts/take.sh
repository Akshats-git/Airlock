#!/usr/bin/env bash
# One command per step of a recording take.
#   scripts/take.sh reset    stop the listeners, restore ERP and bank, start customs, reader, red-team and the ledger
#   scripts/take.sh send     post the four fixture emails into quarantine (needs a running reset)
#   scripts/take.sh status   what is running, and the bank balance
#   scripts/take.sh stop     stop everything this script started
# READER=openai scripts/take.sh reset   uses the real model (needs OPENAI_API_KEY); default is the free recorded records.
# Run it after the Airlock template has created the rooms and agents. Re-running the template for a clean take
# is still done in Console (archive the old ap-quarantine and ap-desk first).
set -euo pipefail
cd "$(dirname "$0")/.."
AGENTS_DIR="${AIRLOCK_AGENTS_DIR:-$HOME/.switch/agents/localhost-43381}"
export AIRLOCK_ERP="$AGENTS_DIR/ap-clerk/erp.sqlite"
export AIRLOCK_BANK="$AGENTS_DIR/treasury/bank.sqlite"
export AIRLOCK_RECEIPTS="$AGENTS_DIR/ap-clerk/inbox/receipts.jsonl"
READER="${READER:-fixture}"
PIDS=runtime/pids LOGS=runtime/logs
mkdir -p "$PIDS" "$LOGS"

stop() {
  for f in "$PIDS"/*.pid; do
    [ -e "$f" ] || continue
    pid=$(cat "$f"); kill "$pid" 2>/dev/null || true; rm -f "$f"
  done
  sleep 1
}

start() { # name, command...
  local name=$1; shift
  nohup "$@" >"$LOGS/$name.log" 2>&1 &
  echo $! >"$PIDS/$name.pid"
}

up() {
  [ "$READER" = openai ] && [ -z "${OPENAI_API_KEY:-}" ] && { echo "READER=openai needs OPENAI_API_KEY exported"; exit 1; }
  start customs  uv run airlock customs
  start reader   uv run airlock reader --backend "$READER"
  start redteam  uv run airlock redteam
  start ledger   uv run bank serve
  sleep 6
  for n in customs reader redteam; do
    grep -q " up\| ready" "$LOGS/$n.log" && echo "$n: up" || { echo "$n: NOT UP"; tail -3 "$LOGS/$n.log"; exit 1; }
  done
  grep -o "using the [a-z]* extractor" "$LOGS/reader.log" | head -1
  echo "ledger: http://localhost:8099"
}

case "${1:-}" in
  reset)  stop; uv run airlock bootstrap >/dev/null; echo "ERP and bank restored"; up ;;
  send)   uv run airlock mailroom send ;;
  status) for f in "$PIDS"/*.pid; do [ -e "$f" ] && { n=$(basename "$f" .pid); kill -0 "$(cat "$f")" 2>/dev/null && echo "$n: running" || echo "$n: dead"; }; done
          "$AGENTS_DIR/treasury/bin/bank" balance ;;
  stop)   stop; echo stopped ;;
  *)      sed -n 2,9p "$0"; exit 1 ;;
esac
