"""The mailroom: posts each vendor email into quarantine. Plain code, takes no instructions.

For every email it posts two messages, each starting with a line the mailroom wrote itself:
an ENVELOPE for customs (the sender domain from the From header, validated by the same rule
customs uses) and a MAIL for the reader (the raw email below a header and a divider).
It needs no WebSocket: posting is plain HTTP.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from email import message_from_string

from airlock.agents import wire
from airlock.agents.common import Names, load_agent, new_run_id, resolve_room
from airlock.customs.gate import envelope_domain
from airlock.protocol import SwitchAgent


@dataclass(frozen=True)
class Mail:
    mail_id: str
    text: str


def mail_id_for(path: Path) -> str:
    """A pattern-bounded id from the file name, e.g. 01-acme.eml -> 01-acme."""
    ident = re.sub(r"[^a-z0-9-]+", "-", path.stem.lower()).strip("-")[:40]
    if not wire.valid_mail_id(ident):
        raise ValueError(f"cannot make a mail id from {path.name!r}")
    return ident


def load_mail(directory: Path, only: list[str] | None = None) -> list[Mail]:
    files = sorted(directory.glob("*.eml"))
    mails = [Mail(mail_id_for(f), f.read_text()) for f in files]
    if only:
        wanted = set(only)
        mails = [m for m in mails if m.mail_id in wanted]
        missing = wanted - {m.mail_id for m in mails}
        if missing:
            raise FileNotFoundError(f"no such mail in {directory}: {', '.join(sorted(missing))}")
    if not mails:
        raise FileNotFoundError(f"no .eml files in {directory}")
    ids = [m.mail_id for m in mails]
    if len(set(ids)) != len(ids):
        raise ValueError("two files produce the same mail id")
    return mails


def sender_domain(email_text: str) -> str | None:
    """The domain of the From header, or None when it is not a plain domain."""
    return envelope_domain(message_from_string(email_text).get("From", ""))


def plan(mails: list[Mail], names: Names, run: str) -> list[str]:
    """The messages to post, in order: for each mail, its ENVELOPE then its MAIL."""
    out: list[str] = []
    for m in mails:
        out.append(wire.format_envelope(names.customs, run, len(mails), m.mail_id, sender_domain(m.text)))
        out.append(wire.format_mail(names.reader, run, m.mail_id, m.text))
    return out


def send(agent: SwitchAgent, quarantine_id: str, mails: list[Mail], names: Names, *, run: str | None = None,
         delay: float = 0.0, now: datetime | None = None, sleep=time.sleep) -> str:
    run = run or new_run_id(now)
    for i, text in enumerate(plan(mails, names, run)):
        if i and delay:
            sleep(delay)
        agent.post(quarantine_id, text)
    return run


def run_send(directory: Path, only: list[str] | None, delay: float, names: Names | None = None) -> str:
    names = names or Names.from_env()
    agent = load_agent(names.mailroom)
    quarantine = resolve_room(agent, names.quarantine)
    return send(agent, quarantine, load_mail(directory, only), names, delay=delay)
