"""The customs agent: the only thing that posts into the AP desk.

``Intake`` is the whole decision logic and touches no network, so it is tested directly.
``run`` wires it to Switch. Customs never copies text from an email or from the reader: it posts
validated values, ids it assigned and fixed reason codes (see ``airlock.customs.gate``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from airlock.agents import wire
from airlock.agents.common import Names, apost, load_agent, resolve_room, same_agent, serve
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
        return Card(rid, text, cleared, len(decisions) - cleared)


async def run(names: Names | None = None) -> None:
    names = names or Names.from_env()
    agent = load_agent(names.customs)
    quarantine = await asyncio.to_thread(resolve_room, agent, names.quarantine)
    desk = await asyncio.to_thread(resolve_room, agent, names.desk)
    agent.rooms = (quarantine, desk)

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
            await apost(agent, desk, card.text)

    async def on_message(msg: Message) -> None:
        if msg.room_id != quarantine:
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

    await serve(agent, on_message, banner=f"customs up: {names.quarantine} -> {names.desk}", extra=sweep)

