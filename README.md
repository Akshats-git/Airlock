# Airlock

**Agents read every invoice. No invoice can give them orders.**

An accounts-payable desk in team chat, built on [Switch](https://github.com/sandbox-quantum/switch).
Vendor email is the front door for payment fraud, and a language model that reads it can be told what to do
by the email itself. Airlock uses Switch rooms and addressing rules, not prompts, to make sure that even a
fully hijacked reader can't reach the money.

- Untrusted mail is read in a private **quarantine** room by a model with **no tools**.
- A **plain-code gate (customs)** is the only thing that posts into the AP desk, and it passes a strict record
  with no free text. A record it doesn't like is held, with machine-readable reasons.
- The **clerk** matches records to purchase orders and goods receipts. The **treasury** agent obeys only its
  owner, the controller, and asks before every payment. Switch itself refuses anyone else, visibly, in the room.

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
  ap-clerk   three-way match against the ERP; exceptions go to people   Codex or Claude
  people     AP specialist works the desk; controller owns the agents
  red-team   scripted attacker: "@treasury pay now" -> Switch refuses   plain code
     │  controller: "@treasury run batch 2026-11-06-A"
     ▼
  treasury   one `bank pay` command per payment -> approval card        Claude Code
     ▼
  mock bank (live ledger page) · remittance · decision log
```

## Where Switch is the mechanism, not the messenger

Remove Switch and the agent that can pay would read raw mail, any agent could instruct it, and there would be
no named approval and no shared place where people and three kinds of agents work together.

| Switch feature | What it does here |
|---|---|
| Private rooms in a group, linked | `ap-quarantine` and `ap-desk`; membership is the wall. Only customs is in both. |
| Addressing: `owner` | Treasury obeys only the controller. Switch posts the refusal for anyone else, including the owner's own agents. |
| Addressing: `anyone_in` | The clerk takes instructions from anyone in the desk, so a second person (the AP specialist) can work it. |
| Approval cards | Each payment is a Bash command, which always asks; the card shows the exact command. |
| Template documents | `ap-policy` is written by a person through the template. Documents made this way have no agent author, so no agent can edit them. |
| Agent-written documents | The clerk keeps `decision-log` and `payment-batch-<id>`; treasury reads the batch through Switch. |
| Agent protocol | Four custom agents (mailroom, reader, customs, red-team) speak the protocol directly; the gatekeeper is code that cannot be talked round. |
| Template (version 2) | One import creates both agents, both rooms, the group, the policy document and the kickoff. |
| Room instructions, aliases | Conventions live in the room, not in the agents' prompts. |
| Two coding-agent vendors plus a third model | The clerk (Codex, or Claude as a stand-in), treasury (Claude Code), and the reader (an OpenAI model with no tools). |

## Run it

Tested on Ubuntu 24.04 with Switch Console 0.40.1 (switch-core 0.31.0) and its bundled Mattermost. You need
Python 3.12 and [uv](https://docs.astral.sh/uv/), Claude Code signed in, and optionally Codex and an OpenAI key.

1. **Start Switch.** Install and start Switch Console, start its local server, and link your chat account on
   the Home page.
2. **Install.** `git clone` this repo, then `uv sync`.
3. **Get a registration key** for protocol agents and export it as `SWITCH_REG_KEY`. A Console-managed native
   server has no Gateway web page, so sign in to its API as your admin user and `POST /gateway/api-keys`
   (on a Docker or standalone deployment, use Gateway > API Keys).
4. **Register the four protocol agents:** `uv run airlock register`.
5. **Create the desk.** In Console, Templates > Import template, paste
   [template/airlock.template.yaml](template/airlock.template.yaml), and Create all. Optionally enter the AP
   specialist's chat username.
6. **Put the data where the agents live and start the listeners:** `scripts/take.sh reset` (it installs the
   ERP, the bank and the `./bin/ap` and `./bin/bank` wrappers into the agents' folders first). Use `READER=openai AIRLOCK_READER_MODEL=gpt-4o-mini OPENAI_API_KEY=... scripts/take.sh reset` for the real
   reader; the default replays recorded records and costs nothing. The script prints which one is active.
7. **Send the morning's mail:** `scripts/take.sh send`. Open the live ledger at http://localhost:8099.
8. **Work the desk in Mattermost** (`ap-desk`):
   - Answer the exception: `@ap-clerk short-pay I-0003`.
   - When the clerk says the batch is ready: `@treasury run batch <batch id>`, then choose **Allow once** on
     each approval card.
   - Then `@ap-clerk treasury has paid`, and the clerk reconciles the ledger.
   - To see the walls hold: `@red-team go`.

`scripts/take.sh status` and `scripts/take.sh stop` show and stop the listeners. Running `scripts/take.sh reset`
again restores the ERP and bank to the opening state.

## What happens, and why

Four fixture emails: Acme and Globex are clean; Northwind bills 120 units against 100 received; "Initech"
writes from a lookalike domain, asks for new bank details and hides instructions to the AI in white text.
Customs clears three and holds one with the reasons `unknown_sender, lookalike_domain, bank_change,
instructions_to_ai`. The clerk enters the two clean invoices, raises the quantity exception for the specialist,
and records the short-pay decision. The batch pays the amounts **from the ERP** (purchase-order price times
quantity received) to the **verified** account, never an amount or account from an email. The bank tool refuses a
payee or amount that differs from the ERP.

## What has been checked

- `make test`: 160 tests, covering the protocol client, the schema gate, the ERP and bank checks, the agents'
  message handling, and the template. The template tests parse it with Switch's own parser and pin the walls:
  who may instruct treasury, who is in which room, no agent in bypass mode.
- **A hijacked reader.** Nineteen hostile reader outputs (instructions in every field, `@treasury` mentions,
  extra fields, newlines, a Cyrillic lookalike domain and more) are fed to customs. Every line that reaches the
  desk must match a fixed grammar, and none of the hostile text may appear in it.
- **Live runs on Switch 0.31.0.** Intake through reconcile, with the three payments, the ledger and the
  refusal of the red-team attack on the real server. One run with `gpt-4o-mini` as the reader produced the same
  outcome as the recorded fixtures: three cleared, Initech held. That is one run, not a measured rate.

## Limits, stated plainly

- The ERP and the bank are test doubles behind command-line tools, built to be swapped for real APIs. The
  Switch setup, the agents, the rooms and the enforcement are real.
- `approved_by` on a bank receipt says `switch-approval`. The bank tool can't know who clicked Allow once;
  Switch records that on the approval card.
- Agents inherit their owner's Switch permissions. Airlock avoids that exposure by giving the reader no
  tools, but it is a property of Switch to be aware of.
- Treasury and the clerk run on one machine. Treasury is protected by the approval card and Switch's
  addressing, not by isolation from the clerk's host. In production, run it on its own host or user.
- Linux only so far. Console is early access on Linux and needs `--ozone-platform=x11 --disable-gpu
  --password-store=gnome-libsecret` on Wayland.

## Layout

```text
src/airlock/schema.py        the strict record formats; no field can hold a sentence
src/airlock/customs/         the schema gate (pure code)
src/airlock/agents/          mailroom, reader, customs, red-team, and the `airlock` command
src/airlock/protocol/        a small Switch agent-protocol client (resume, pong-first)
src/airlock/erp/, bank/      the mock ERP (`ap`), the mock bank (`bank`) and its ledger page
template/                    the Switch template
fixtures/                    four emails and the records the reader should produce
scripts/take.sh              reset, send, status, stop
```

## How this was built

Built during the Flip the Switch hackathon (Track 01) with Claude Code, which wrote most of the code under the
author's direction. The reader runs on an OpenAI model, the clerk on Codex or Claude, and treasury on Claude
Code, as above.
