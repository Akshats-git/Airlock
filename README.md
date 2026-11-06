# Airlock

**Agents read every invoice. No invoice can give them orders.**

An accounts-payable desk in team chat, built on [Switch](https://github.com/sandbox-quantum/switch).
Vendor email is the front door for payment fraud, and a language model that reads it can be told what to do
by the email itself. Airlock uses Switch rooms, addressing rules and document authorship, not prompts, to make
sure that even a fully hijacked reader can't reach the money.

- Untrusted mail is read in a private **quarantine** room by a model with **no tools**.
- A **plain-code gate (customs)** is the only thing that posts into the AP desk, and it passes a strict record
  with no free text. A record it doesn't like is held, with machine-readable reasons.
- The **clerk** matches records to purchase orders and goods receipts and hands exceptions to a person. The
  **treasury** agent obeys only its owner, the controller, and asks before every payment. Switch itself refuses
  anyone else, visibly, in the room.

Demo video: _link added at submission_

```text
UNTRUSTED  ap-quarantine (private) ───────────────────────────────────────────
  mailroom   posts each raw email under a header line it wrote       plain code
     │
  reader     one email -> one JSON record; no tools, no memory        a model
     │
  customs    strict schema gate; drops all free text; member of both  plain code
     │
═════╪═══ the airlock: only a validated record crosses ════════════════════════
     ▼
TRUSTED    ap-desk (private) ─────────────────────────────────────────────────
  ap-clerk   three-way match against the ERP; exceptions go to people   Codex (or Claude)
  people     AP specialist works the desk; controller owns the agents
  red-team   scripted attacker: orders treasury, edits documents        plain code
     │  controller: "@treasury run batch 2026-11-07-A"
     ▼
  treasury   one `bank pay` command per payment -> approval card        Claude Code
     ▼
  mock bank (live ledger page) · remittance · decision log
```

## Where Switch is the mechanism, not the messenger

Remove Switch and the agent that can pay would read raw mail, any agent could instruct it, any agent could
rewrite the policy or the batch it pays from, and there would be no named approval and no shared place where
people and three kinds of agents work together.

| Switch feature | What it does here |
|---|---|
| Private rooms in a group, linked | `ap-quarantine` and `ap-desk`; membership is the wall. Only customs is in both. |
| Addressing: `owner` | Treasury obeys only the controller. Switch posts the refusal for anyone else, including the owner's own agents. |
| Addressing: `anyone_in` | The clerk takes instructions from anyone in the desk, so a second person (the AP specialist) can work it. |
| Approval cards | Each payment is a Bash command, which always asks; the card shows the exact command and who chose Allow once. |
| Document authorship | `ap-policy` comes from the template, so no agent wrote it and no agent can change it. The batch belongs to the clerk. The red team tries both and gets Switch's 403. |
| Agent-written documents | The clerk keeps `decision-log` and `payment-batch-<id>`; treasury reads the batch through Switch. |
| Agent protocol | Four custom agents (mailroom, reader, customs, red-team) speak the protocol directly; the gatekeeper is code that cannot be talked round. |
| Template (version 2) | One import creates both coding agents, both rooms, the group and link, the policy document and the kickoff. |
| Room instructions, aliases | Conventions live in the room, not in the agents' prompts. |
| Several vendors | The clerk on Codex (or Claude Code), treasury on Claude Code, the reader on an OpenAI model with no tools. |

## Run it

Tested on Ubuntu 24.04 with Switch Console 0.40.1 (switch-core 0.31.0) and its bundled Mattermost. You need
Python 3.12 and [uv](https://docs.astral.sh/uv/), Claude Code signed in, and optionally Codex and an OpenAI key.
Every command finds the server's port and the agents' folders by itself (`uv run airlock paths` shows them);
set `AIRLOCK_API` or `AIRLOCK_AGENTS_DIR` to override.

1. **Start Switch.** Install and start Switch Console, start its local server, and link your chat account on
   the Home page.
2. **Install.** `git clone` this repo, then `uv sync`.
3. **Get a registration key** for the protocol agents: `uv run airlock reg-key`. It signs in to your server's
   Gateway API as the admin and reuses or creates a key labelled `airlock`, saved to `runtime/registration-key`
   (only you can read it; it is never printed). It prompts for the admin email and password, or reads
   `SWITCH_ADMIN_EMAIL` and `SWITCH_ADMIN_PASSWORD`. A Docker or standalone deployment can instead create one on
   the Gateway's API Keys page and export it as `SWITCH_REG_KEY`. A Console-managed local server has no Gateway
   page, and Console doesn't show its admin sign-in: it hands it to the switch-core process as the environment
   variables `GATEWAY_ADMIN_EMAIL` and `GATEWAY_ADMIN_PASSWORD`.
4. **Register the four protocol agents:** `uv run airlock register`.
5. **Create the desk.** In Console, Templates > Import template, paste
   [template/airlock.template.yaml](template/airlock.template.yaml), and Create all. Keep "Clerk runs on" as
   Codex for the sandboxed clerk. Optionally enter the AP specialist's chat username.
6. **Put the data where the agents live and start the listeners:** `scripts/take.sh reset`. It installs the
   ERP, the bank and the `./bin/ap` and `./bin/bank` tools into the agents' folders, then starts customs, the
   reader, the red team and the ledger page. For the real reader model:
   `READER=openai AIRLOCK_READER_MODEL=gpt-4o-mini OPENAI_API_KEY=... scripts/take.sh reset`; the default
   replays recorded records and costs nothing.
7. **Check everything:** `scripts/take.sh doctor`. It asks the live server who is in which room, whether
   treasury obeys its owner only, and which provider runs the clerk, and checks the files and listeners.
8. **Send the morning's mail:** `scripts/take.sh send`. Open the live ledger at http://localhost:8099.
9. **Work the desk in Mattermost** (`ap-desk`):
   - Answer the exception: `@ap-clerk short-pay I-0003`.
   - When the clerk says the batch is ready: `@treasury run batch <batch id>`, then choose **Allow once** on
     each approval card (it may open in the thread under your message).
   - Then `@ap-clerk treasury has paid`, and the clerk reconciles the ledger.

For a second take, use brand-new rooms: `scripts/take.sh fresh` lists the steps. A reused room still holds
the earlier batch, and the clerk rightly asks whether it is a re-run.

## See the walls hold

Mention the red team in `ap-desk`. Each attack is scripted, so the outcome never depends on a model being
fooled, and every refusal comes from the Switch server.

| Say | The attack | What stops it |
|---|---|---|
| `@red-team 1` … `4` | Orders treasury to pay: "the CFO approved", "controller is travelling", a new IBAN, a raw `bank pay` command | Treasury's `owner` addressing. Switch replies in the room: "You're not permitted to direct messages to me in this room". |
| `@red-team policy` (5) | Rewrites `ap-policy` so treasury also takes orders from the clerk | Document authorship. Switch answers 403 "did not create document", and the red team posts that answer as-is. |
| `@red-team batch` (6) | Swaps the payee on the newest payment batch to Initech | The same 403. Had it got through, `bank pay` would still refuse a payee or amount that differs from the ERP. |

The fraud email itself never reaches the desk: customs holds it with
`unknown_sender, lookalike_domain, bank_change, instructions_to_ai`.

## What happens, and why

Four fixture emails: Acme and Globex are clean; Northwind bills 120 units against 100 received; "Initech"
writes from a lookalike domain, asks for new bank details and hides instructions to the AI in white text.
Customs clears three and holds one. It decides `unknown_sender` itself, by looking the sender's domain up in the
vendor master, so the hold doesn't depend on the reader noticing anything. The clerk enters the two clean
invoices, raises the quantity exception for the specialist, and records the short-pay decision. The batch pays
the amounts **from the ERP** (purchase-order price times quantity received) to the **verified** account, never an
amount or account from an email. The bank tool refuses a payee or amount that differs from the ERP.

## What has been checked

- `make test`: 187 tests, covering the protocol client, the schema gate, the ERP and bank checks, the agents'
  message handling, the red team, the setup tools and the template. The template tests parse it with Switch's
  own parser and pin the walls: who may instruct treasury, who is in which room, no agent in bypass mode.
- **A hijacked reader.** Nineteen hostile reader outputs (instructions in every field, `@treasury` mentions,
  extra fields, newlines, a Cyrillic lookalike domain and more) are fed to customs. Every line that reaches the
  desk must match a fixed grammar, and none of the hostile text may appear in it.
- **The clerk's sandbox.** [tests/test_isolation.py](tests/test_isolation.py) runs the real `bank pay` from the
  clerk's folder: without a sandbox it pays; inside Codex's `workspace-write` sandbox, which Switch gives a
  Codex agent, nothing moves.
- **Live runs on Switch 0.31.0.** Intake through reconcile, with the three payments, Allow once and Decline on
  the approval cards, the ledger, the refusal of every red-team order to treasury, and Switch's 403 on both
  document attacks. The latest full run had the clerk on Codex (`gpt-6-luna`): three cleared, one held, one
  exception short-paid, three payments approved with Allow once, reconciled, then both document attacks
  refused. One run with `gpt-4o-mini` as the reader produced the same outcome as the recorded
  fixtures: three cleared, Initech held. That is one run, not a measured rate.

## Limits, stated plainly

- **The clerk and treasury share a machine and an OS user.** With the clerk on Codex (the template's default),
  Switch runs it in Codex's `workspace-write` sandbox, and the isolation test shows the bank tool can't write its
  ledger from there. In Codex's auto mode a request to leave the sandbox goes to Codex's automatic reviewer,
  not to a person. With the clerk on Claude Code there is no sandbox: the clerk could run treasury's bank tool
  itself and pay without an approval card, and only the model's own judgment stops it. The full run has been
  done live with the clerk on Codex (`gpt-6-luna`) and earlier ones with Claude Code. For real money, run treasury on its own machine (Switch's `machine`
  setting), so its tool isn't reachable from the clerk at all.
- The ERP and the bank are test doubles behind command-line tools, built to be swapped for real APIs. The
  Switch setup, the agents, the rooms and the enforcement are real.
- `approved_by` on a bank receipt says `switch-approval`. The bank tool can't know who clicked Allow once;
  Switch records that on the approval card.
- Agents inherit their owner's Switch permissions. Airlock gives the reader no tools, and customs and the
  mailroom are plain code, but it is a property of Switch to be aware of.
- Linux only so far. Console is early access on Linux and needs `--ozone-platform=x11 --disable-gpu
  --password-store=gnome-libsecret` on Wayland.

## Other chat apps

Tested on the bundled Mattermost only. The template's `bridge` setting picks the messaging app, and every wall
here lives in Switch (rooms, addressing, document authorship, approval cards), not in the chat app, so the design
doesn't change on Slack, Discord, Teams or Telegram. Where an approval button can't be shown, typing the card's
answer (for example `A3 1`) works the same way.

## Troubleshooting

| You see | Do this |
|---|---|
| An agent stays silent | `scripts/take.sh doctor`; `!agents-status` in the room; open the thread under your message |
| The clerk asks "Is this a re-run?" | The room still holds an earlier batch: `scripts/take.sh fresh` |
| Treasury says there is no batch document | The clerk hasn't proposed one yet, or the batch id is mistyped |
| No approval card | Open the thread under `@treasury run batch`; check treasury's Auto mode is off |
| `airlock` can't find the server | Start the local server in Console, or set `AIRLOCK_API` |

## Layout

```text
src/airlock/schema.py        the strict record formats; no field can hold a sentence
src/airlock/customs/         the schema gate (pure code)
src/airlock/agents/          mailroom, reader, customs, red-team, and the `airlock` command
src/airlock/protocol/        a small Switch agent-protocol client (resume, pong-first)
src/airlock/erp/, bank/      the mock ERP (`ap`), the mock bank (`bank`) and its ledger page
src/airlock/doctor.py        `airlock doctor`: the walls, checked against the live server
src/airlock/local.py         finds the server's port and the agents' folders
src/airlock/gateway.py       `airlock reg-key`: a registration key from your own server
template/                    the Switch template
fixtures/                    four emails and the records the reader should produce
scripts/take.sh              reset, send, doctor, status, stop, fresh
```

## How this was built

Built during the Flip the Switch hackathon (Track 01) with Claude Code, which wrote most of the code under the
author's direction. The reader runs on an OpenAI model, the clerk on Codex or Claude, and treasury on Claude
Code, as above.
