#!/usr/bin/env bash
# First-time setup on a machine that already runs Switch Console with its local server. Run it yourself, in your
# own terminal: step 2 reads the server's admin sign-in from the switch-core process you started, which an agent
# must not do for you. Safe to run again; each step skips what is already done.
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n== %s\n' "$*"; }

say "1/5 Install"
command -v uv >/dev/null || { echo "Install uv first: https://docs.astral.sh/uv/"; exit 1; }
uv sync

say "2/5 Registration key for the four protocol agents"
if [ -s runtime/registration-key ]; then
  echo "runtime/registration-key exists; skipping."
else
  pid=$(pgrep -f switch_core.main | head -1 || true)
  if [ -n "$pid" ] && [ -r "/proc/$pid/environ" ]; then
    SWITCH_ADMIN_EMAIL="$(tr '\0' '\n' <"/proc/$pid/environ" | sed -n 's/^GATEWAY_ADMIN_EMAIL=//p')"
    SWITCH_ADMIN_PASSWORD="$(tr '\0' '\n' <"/proc/$pid/environ" | sed -n 's/^GATEWAY_ADMIN_PASSWORD=//p')"
    export SWITCH_ADMIN_EMAIL SWITCH_ADMIN_PASSWORD
  else
    echo "Couldn't read the admin sign-in from a running switch-core; you will be asked for it."
  fi
  uv run airlock reg-key
fi

say "3/5 Register mailroom, reader, customs and red-team"
uv run airlock register

say "4/5 Create the desk in Switch Console (the one step that needs clicks)"
cat <<'EOT'
  1. Console > Templates > Import template, and choose template/airlock.template.yaml in this folder.
     (An older Airlock template there? Open it, paste the file again, Save changes.)
  2. Use template, then Create all.
     If a second person works the desk, enter their chat username as "AP specialist", and also run:
       uv run airlock specialist <username>
  3. Open treasury's settings: Auto mode off, "Who can talk to your agent" = Only me.
EOT
read -r -p "Press Enter when the desk exists... " _

say "5/5 Put the ERP and bank where the agents live, start the listeners, check the walls"
scripts/take.sh reset
scripts/take.sh doctor || { echo "Fix the lines marked ✗ above, then run scripts/take.sh doctor again."; exit 1; }

cat <<'EOT'

Ready. Next:
  scripts/take.sh deliver        drop the four demo emails into the inbox (MAIL=fixtures/morning for 30)
  then work the desk in Mattermost (README, "Run it", step 9), and finish with scripts/take.sh verify.
For a second take in new rooms: scripts/take.sh fresh
EOT
