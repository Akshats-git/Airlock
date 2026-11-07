"""The customs agent: the only thing that posts into the AP desk.

``Intake`` is the whole decision logic and touches no network, so it is tested directly.
``run`` wires it to Switch. Customs never copies text from an email or from the reader: it posts
validated values, ids it assigned and fixed reason codes (see ``airlock.customs.gate``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from airlock.agents import wire
from airlock.agents.common import Names, apost, load_agent, resolve_room, same_agent, serve
from airlock.bank import ops as bank_ops
from airlock.customs import gate
from airlock.erp.db import connect, erp_path, today
from airlock.protocol import Message

log = logging.getLogger("airlock.customs")

RUN_TIMEOUT_SECONDS = 90.0


@dataclass
class _Run:
    n: int
    started: float
    order: list[str] = field(default_factory=list)  # mail ids in the order their envelopes arrived
    envelopes: dict[str, str | None] = field(default_factory=dict)
    payloads: dict[str, str] = field(default_factory=dict)
    done: bool = False


@dataclass(frozen=True)
class Card:
    """Something to post into the desk. ``text`` already carries the mention when one is wanted."""

    run: str
    text: str
    cleared: int
    held: int
    holds: tuple[gate.Held, ...] = ()


class Intake:
    """Collect one run's envelopes and reader results, then decide the whole run at once.

    A run is decided exactly once: when every mail has an envelope and a result, or when it
    times out (a missing result is held as ``schema_invalid``). Later messages for a decided run
    are ignored, so a replayed or forged message cannot reopen it.
    """

    def __init__(self, vendors: Callable[[], list[gate.Vendor]], names: Names, *, day: date | None = None,
                 clock: Callable[[], float] = time.monotonic, timeout: float = RUN_TIMEOUT_SECONDS):
        self._vendors, self._names, self._day = vendors, names, day
        self._clock, self._timeout = clock, timeout
        self._runs: dict[str, _Run] = {}

    # -- inputs ---------------------------------------------------------------------------
    def on_envelope(self, env: wire.Envelope) -> Card | None:
        run = self._runs.get(env.run)
        if run is None:
            run = self._runs[env.run] = _Run(env.total, self._clock())
        if run.n == 0:
            run.n = env.total
        if run.done or env.mail in run.envelopes:
            return None
        run.order.append(env.mail)
        run.envelopes[env.mail] = env.domain
        return self._maybe_finish(env.run)

    def on_read(self, res: wire.ReadResult) -> Card | None:
        run = self._runs.get(res.run)
        if run is None:
            run = self._runs[res.run] = _Run(0, self._clock())
        if run.done or res.mail in run.payloads:
            return None
        run.payloads[res.mail] = res.payload
        return self._maybe_finish(res.run)

    def expire(self) -> list[Card]:
        """Decide any run that has waited too long. Call this on a timer."""
        out = []
        for rid, run in self._runs.items():
            if not run.done and self._clock() - run.started >= self._timeout:
                card = self._finish(rid)
                if card:
                    out.append(card)
        return out

    # -- decisions ------------------------------------------------------------------------
    def _maybe_finish(self, rid: str) -> Card | None:
        run = self._runs[rid]
        ready = run.n and len(run.envelopes) >= run.n and all(m in run.payloads for m in run.order)
        return self._finish(rid) if ready else None

    def _finish(self, rid: str) -> Card | None:
        run = self._runs[rid]
        if run.done:
            return None
        run.done = True
        mails = run.order or sorted(run.payloads)  # results with no envelope still get a decision
        day = self._day or today()
        vendors = self._vendors()
        decisions = []
        for seq, mail in enumerate(mails, start=1):
            payload = run.payloads.get(mail, "")
            envelope = run.envelopes.get(mail)  # a mail with no envelope is held by the gate
            decisions.append(gate.check(payload, envelope, vendors, day, seq))
        cleared = sum(isinstance(d, gate.Cleared) for d in decisions)
        text = gate.render_card(decisions, day, rid)  # type: ignore[arg-type]  # the run id is a string
        if cleared:
            text = f"@{self._names.clerk} {text}"
        return Card(rid, text, cleared, len(decisions) - cleared,
                    tuple(d for d in decisions if isinstance(d, gate.Held)))


def record_holds(card: Card, path: Path | None = None) -> None:
    """Append the run's held records to runtime/holds.jsonl: ids, the vendor (if the sender is one) and reason
    codes, never a word of the email. The postman tells a vendor when mail from its domain asked for new bank
    details."""
    from airlock.agents.common import runtime_dir
    if not card.holds:
        return
    path = path or runtime_dir() / "holds.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for h in card.holds:
            f.write(json.dumps({"record_id": h.record_id, "vendor_id": h.vendor_id, "reasons": list(h.reasons),
                                "run": card.run}) + "\n")


NOT_SPECIALIST = "only the AP specialist, a person, can record a bank-detail callback"


def desk_line(sender: str, body: str, specialist: str | None,
              record: Callable[[str, str, str, str], str]) -> str | None:
    """Customs' second job: notarise the AP specialist's callback for a bank-detail change.

    Who posted it comes from Switch's sender id, never from the message or a model. Returns the line to post,
    or None when the message isn't a VERIFIED line at all (ordinary desk chat, which customs ignores).
    ``record(vendor, iban, phone, verified_by)`` writes it to the bank and returns the bank's line.
    """
    v = wire.parse_verified(body)
    if v is None:
        return None
    who = wire.person_username(sender)
    if not specialist or who is None or who.casefold() != specialist.casefold():
        return f"NOT RECORDED {v.vendor}: {NOT_SPECIALIST}"
    try:
        return record(v.vendor, v.iban, v.phone, who)
    except bank_ops.BankError as e:
        return f"NOT RECORDED {v.vendor}: {e}"


def specialist_name() -> str | None:
    """$AIRLOCK_SPECIALIST, else the username `airlock specialist` saved."""
    from airlock.agents.common import runtime_dir
    env = os.environ.get("AIRLOCK_SPECIALIST")
    if env:
        return env.strip()
    try:
        return (runtime_dir() / "specialist").read_text().strip() or None
    except OSError:
        return None


async def run(names: Names | None = None) -> None:
    names = names or Names.from_env()
    agent = load_agent(names.customs)
    quarantine = await asyncio.to_thread(resolve_room, agent, names.quarantine)
    desk = await asyncio.to_thread(resolve_room, agent, names.desk)
    agent.rooms = (quarantine, desk)
    agent.only_addressed = False  # the specialist's VERIFIED line mentions nobody; quarantine still needs a mention
    specialist = specialist_name()

    def record(vendor: str, iban: str, phone: str, who: str) -> str:
        bank = bank_ops.connect()
        try:
            return bank_ops.record_verification(bank, vendor, iban, phone, who)
        finally:
            bank.close()

    def vendors() -> list[gate.Vendor]:
        conn = connect(erp_path(), readonly=True)
        try:
            return gate.load_vendors(conn)
        finally:
            conn.close()

    intake = Intake(vendors, names)

    async def deliver(card: Card | None) -> None:
        if card:
            log.info("run %s decided: cleared=%d held=%d", card.run, card.cleared, card.held)
            record_holds(card)
            await apost(agent, desk, card.text)

    async def on_message(msg: Message) -> None:
        if msg.room_id == desk:
            line = await asyncio.to_thread(desk_line, msg.sender, msg.body, specialist, record)
            if line:
                log.info("desk: %s", line)
                await apost(agent, desk, line)
            return
        if msg.room_id != quarantine or not msg.addressed:
            return
        if same_agent(msg.sender_name, names.mailroom):
            env = wire.parse_envelope(msg.body)
            if env is not None:
                await deliver(intake.on_envelope(env))
        elif same_agent(msg.sender_name, names.reader):
            res = wire.parse_read(msg.body)
            if res is not None:
                await deliver(intake.on_read(res))
        else:
            log.warning("ignored a message from %r", msg.sender_name)

    async def sweep(stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(2.0)
            for card in intake.expire():
                await deliver(card)

    await serve(agent, on_message, banner=f"customs up: {names.quarantine} -> {names.desk}; bank-detail callbacks "
                                          f"from {specialist or 'nobody (no AP specialist set)'}", extra=sweep)

