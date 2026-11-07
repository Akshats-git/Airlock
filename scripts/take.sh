#!/usr/bin/env bash
# One command per step of a run.
#   scripts/take.sh reset    stop the listeners, restore ERP and bank, start customs, reader, red-team and the ledger
#   scripts/take.sh send     post the four fixture emails into quarantine (needs a running reset)
#   scripts/take.sh deliver  drop mail into runtime/inbox/new; the always-on mailroom posts it (MAIL=fixtures/morning)
#   scripts/take.sh doctor   check the walls and the setup against the live server
#   scripts/take.sh verify   after a run: ledger, ERP, payment files, holds and letters agree, and no harm
#   scripts/take.sh mailpit  start a local mail server; letters to vendors then show at http://localhost:8025
#   scripts/take.sh status   what is running, and the bank balance
#   scripts/take.sh stop     stop everything this script started
#   scripts/take.sh fresh    archive the old rooms (Switch and Mattermost), then the Console steps for new ones
# First time on a machine: scripts/setup.sh
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
  rm -rf runtime/inbox runtime/outbox runtime/holds.jsonl; mkdir -p runtime/inbox/new runtime/inbox/cur runtime/inbox/tmp
  start mailroom uv run airlock mailroom watch --inbox runtime/inbox
  if mailpit_up; then
    curl -s -X DELETE "http://localhost:$MAILPIT_WEB/api/v1/messages" >/dev/null || true
    export AIRLOCK_SMTP="localhost:$MAILPIT_SMTP"
  fi
  start postman  uv run airlock postman
  sleep 6
  for n in customs reader redteam mailroom postman; do
    grep -q " up\| ready\| watching" "$LOGS/$n.log" && echo "$n: up" || { echo "$n: NOT UP"; tail -3 "$LOGS/$n.log"; exit 1; }
  done
  grep -o "using the [a-z]* extractor" "$LOGS/reader.log" | head -1
  echo "ledger: http://localhost:8099"
  mailpit_up && echo "letters to vendors: http://localhost:$MAILPIT_WEB" || echo "letters to vendors: runtime/outbox (scripts/take.sh mailpit for an inbox page)"
  echo "next: scripts/take.sh doctor, then scripts/take.sh send"
}

MAILPIT_WEB=8025 MAILPIT_SMTP=1025
mailpit_up() { docker ps --format '{{.Names}}' 2>/dev/null | grep -qx airlock-mailpit; }
mailpit() {
  mailpit_up || docker run -d --rm --name airlock-mailpit -p "127.0.0.1:$MAILPIT_WEB:8025" \
    -p "127.0.0.1:$MAILPIT_SMTP:1025" ghcr.io/axllent/mailpit:v1.27 >/dev/null
  echo "mailpit: http://localhost:$MAILPIT_WEB (run scripts/take.sh reset so the postman sends there)"
}

fresh() {
  stop
  echo "Archiving the old rooms, in Switch and in Mattermost (it asks first):"
  uv run airlock archive-rooms || echo "(nothing archived; continue if there were no old rooms)"
  cat <<'EOF'
Now, in Console (old messages and documents in a reused room confuse the clerk):
  1. Agents: open ap-clerk, Danger zone > Delete agent..., then the same for treasury
     (the template refuses names that exist). The four protocol agents stay registered; don't delete them.
  2. Templates > Import template: choose template/airlock.template.yaml (Save changes if the
     template already exists), Use template, then Create all.
     If a second person works the desk, put their chat username in "AP specialist" (once per account,
     also run: uv run airlock specialist <username>).
  3. In treasury's settings, check Auto mode is off and "Who can talk to your agent" is Only me.
Then:
  scripts/take.sh reset      (installs the ERP, bank and tools into the new agents' folders)
  scripts/take.sh doctor     (every line should be ok before you record;
                              AIRLOCK_SPECIALIST=<username> scripts/take.sh doctor also checks the specialist's seat)
  scripts/take.sh send       (or deliver)
EOF
}

case "${1:-}" in
  reset)  stop; locate; uv run airlock bootstrap >/dev/null; echo "ERP and bank restored in $AIRLOCK_AGENTS_DIR"; up ;;
  send)   uv run airlock mailroom send --directory "${MAIL:-fixtures/emails}" ;;
  deliver) for f in "${MAIL:-fixtures/emails}"/*.eml; do cp "$f" "runtime/inbox/tmp/$(basename "$f")"; mv "runtime/inbox/tmp/$(basename "$f")" runtime/inbox/new/; done
           echo "delivered $(ls runtime/inbox/new | wc -l) emails to runtime/inbox/new; the mailroom posts them within seconds" ;;
  doctor) uv run airlock doctor ;;
  verify) locate; uv run airlock verify ;;
  mailpit) mailpit ;;
  status) locate
          for f in "$PIDS"/*.pid; do [ -e "$f" ] && { n=$(basename "$f" .pid); kill -0 "$(cat "$f")" 2>/dev/null && echo "$n: running" || echo "$n: dead"; }; done
          "$AIRLOCK_AGENTS_DIR/treasury/bin/bank" balance ;;
  stop)   stop; docker stop airlock-mailpit >/dev/null 2>&1 || true; echo stopped ;;
  fresh)  fresh ;;
  *)      sed -n 2,12p "$0"; exit 1 ;;
esac
