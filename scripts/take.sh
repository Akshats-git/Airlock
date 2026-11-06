#!/usr/bin/env bash
# One command per step of a take.
#   scripts/take.sh reset    stop the listeners, restore ERP and bank, start customs, reader, red-team and the ledger
#   scripts/take.sh send     post the four fixture emails into quarantine (needs a running reset)
#   scripts/take.sh doctor   check the walls and the setup against the live server
#   scripts/take.sh status   what is running, and the bank balance
#   scripts/take.sh stop     stop everything this script started
#   scripts/take.sh fresh    the Console steps for a take in brand-new rooms
# READER=openai scripts/take.sh reset   uses the real model (needs OPENAI_API_KEY); default is the free recorded records.
# Paths are found automatically (`uv run airlock paths`); set AIRLOCK_API or AIRLOCK_AGENTS_DIR to override.
set -euo pipefail
cd "$(dirname "$0")/.."
READER="${READER:-fixture}"
PIDS=runtime/pids LOGS=runtime/logs
mkdir -p "$PIDS" "$LOGS"

locate() {
  local out
  out=$(uv run airlock paths --shell) || { echo "$out"; exit 1; }
  eval "$out"
}

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
  echo "next: scripts/take.sh doctor, then scripts/take.sh send"
}

fresh() {
  cat <<'EOF'
A take in brand-new rooms (old messages and documents in a reused room confuse the clerk):
  1. scripts/take.sh stop
  2. uv run airlock archive-rooms   (archives the old rooms in Switch; do this BEFORE step 4)
     Then archive the same two channels in Mattermost so they leave the sidebar.
  3. In Console, Agents: open ap-clerk, Danger zone > Delete agent..., then the same for treasury
     (the template refuses names that exist). The four protocol agents stay registered; don't delete them.
  4. Console > Templates > Import template: choose template/airlock.template.yaml (Save changes if the
     template already exists), Use template, then Create all. Leave "Clerk runs on" as Codex.
     If a second person works the desk, put their chat username in "AP specialist" (once per account,
     also run: uv run airlock specialist <username>).
  5. In treasury's settings, check Auto mode is off and "Who can talk to your agent" is Only me.
  6. scripts/take.sh reset      (installs the ERP, bank and tools into the new agents' folders)
  7. scripts/take.sh doctor     (every line should be ✓ before you record;
     AIRLOCK_SPECIALIST=<username> scripts/take.sh doctor also checks the specialist's seat)
  8. scripts/take.sh send
EOF
}

case "${1:-}" in
  reset)  stop; locate; uv run airlock bootstrap >/dev/null; echo "ERP and bank restored in $AIRLOCK_AGENTS_DIR"; up ;;
  send)   uv run airlock mailroom send ;;
  doctor) uv run airlock doctor ;;
  status) locate
          for f in "$PIDS"/*.pid; do [ -e "$f" ] && { n=$(basename "$f" .pid); kill -0 "$(cat "$f")" 2>/dev/null && echo "$n: running" || echo "$n: dead"; }; done
          "$AIRLOCK_AGENTS_DIR/treasury/bin/bank" balance ;;
  stop)   stop; echo stopped ;;
  fresh)  fresh ;;
  *)      sed -n 2,10p "$0"; exit 1 ;;
esac
