# Running the desk on Switch

The [README](../README.md) covers the offline path, which needs no Switch. This page sets up the live desk.

## Requirements

| | |
|---|---|
| Switch | Switch Console 0.40.1 (switch-core 0.31.0) with its bundled Mattermost |
| OS | Tested on Ubuntu 24.04. On Wayland, start Console with `--ozone-platform=x11 --disable-gpu --password-store=gnome-libsecret` |
| Tools | Python 3.12, [uv](https://docs.astral.sh/uv/), Claude Code signed in, Codex |
| Optional | An OpenAI key for the real reader model; Docker for a local Mailpit inbox |

Every command finds the server's port and the agents' folders on its own (`uv run airlock paths` shows them).
Set `AIRLOCK_API` or `AIRLOCK_AGENTS_DIR` to override.

## 1. Set up once

1. Start Switch Console and its local server, then link your chat account on the Home page.
2. In your own terminal, run `scripts/setup.sh` and follow the prompts. It:
   - runs `uv sync`
   - gets a Gateway registration key and registers the four protocol agents (mailroom, reader, customs, red-team)
   - lists the Console clicks that create the desk from [the template](../template/airlock.template.yaml):
     **Templates > Import template > Use template > Create all**, then set treasury to **Auto mode off** and
     **Only me**
   - runs `scripts/take.sh reset` and `scripts/take.sh doctor`

Run `setup.sh` yourself rather than through an agent: a Console-managed server gives its admin sign-in only to
the switch-core process, and the script reads it from there. On a Docker or standalone server, create the key on
the Gateway's API Keys page instead and export it as `SWITCH_REG_KEY`.

`reset` installs the ERP, the bank and the `./bin/ap` and `./bin/bank` tools into the agents' folders, then starts
customs, the reader, the red team, the mailroom (watching `runtime/inbox`), the postman and the ledger page.

| Option | How |
|---|---|
| Real reader model | `READER=openai AIRLOCK_READER_MODEL=gpt-4o-mini OPENAI_API_KEY=... scripts/take.sh reset` (the default replays recorded records and costs nothing) |
| Letters in an inbox page | `scripts/take.sh mailpit` before `reset` (http://localhost:8025); otherwise they land in `runtime/outbox/` |
| A second person | See [a second person at the desk](DETAILS.md#a-second-person-at-the-desk) |

## 2. Run a morning

1. **Deliver mail:** `scripts/take.sh deliver` (four emails) or `MAIL=fixtures/morning scripts/take.sh deliver`
   (the 30-email morning). The ledger is at http://localhost:8099.
2. **Work the desk** in Mattermost, channel `ap-desk`:
   - answer the exception: `@ap-clerk short-pay I-0003`
   - when the batch is ready: `@treasury run batch <batch id>`, then **Allow once** on each card in the thread
   - afterwards: `@ap-clerk treasury has paid`, and the clerk reconciles
3. **Check the result:** `scripts/take.sh verify`. It confirms that the ledger, the ERP, the payment files and the
   letters agree, and that the attack corpus's judge finds no harm.

For another run, start from fresh rooms with `scripts/take.sh fresh`. A reused room still holds the earlier batch,
and the clerk will rightly ask whether it is a re-run.

## 3. Try the attacks

Mention the red team in `ap-desk`. The attacks are scripted, so the outcome never depends on a model being
fooled, and every refusal comes from the Switch server.

| Say | Attack | What stops it |
|---|---|---|
| `@red-team 1` to `4` | Orders treasury to pay ("the CFO approved", a new IBAN, a raw `bank pay` command) | Treasury's owner-only addressing. Switch refuses in the room. |
| `@red-team policy` | Rewrites `ap-policy` so treasury also obeys the clerk | Document authorship: Switch answers 403. |
| `@red-team batch` | Swaps the payee on the newest payment batch | The same 403. The bank would also refuse a payee that differs from the ERP. |
| `@red-team account` | Plays a hijacked clerk and tries to add the attacker's account to the bank's list | Codex's sandbox: "attempt to write a readonly database". |
| `@red-team callback` | Forges the AP specialist's bank-detail callback | Customs records a callback only when Switch says a person, the specialist, sent it. |

## `scripts/take.sh`

| Command | Does |
|---|---|
| `reset` | Stop the listeners, restore the ERP and the bank, start everything again |
| `deliver` | Drop mail into `runtime/inbox/new` for the mailroom |
| `doctor` | Check the walls and the setup against the live server |
| `verify` | Check a finished run from the records |
| `status`, `stop` | Show or stop the listeners |
| `fresh` | Archive the old rooms and list the Console steps for new ones |
| `mailpit` | Start a local inbox for the postman's letters |

## Troubleshooting

| You see | Do this |
|---|---|
| An agent stays silent | `scripts/take.sh doctor`; `!agents-status` in the room; open the thread under your message |
| The clerk asks "Is this a re-run?" | The room still holds an earlier batch: `scripts/take.sh fresh` |
| Treasury says there is no batch document | The clerk hasn't proposed one yet, or the batch id is mistyped |
| No approval card | Open the thread under `@treasury run batch`; check that treasury's Auto mode is off |
| `airlock` can't find the server | Start the local server in Console, or set `AIRLOCK_API` |
