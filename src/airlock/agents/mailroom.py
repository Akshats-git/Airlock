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


# -- always on: watch an inbox folder ----------------------------------------------------------------------
# A Maildir-style folder: a mail fetcher (fetchmail, getmail, a forwarding rule) drops each message in new/;
# the mailroom posts everything that has arrived as one run, then files it in cur/. Nothing else reads it.

MAX_PER_RUN = 99  # the ENVELOPE header carries the run size in two digits


def _mail_id(path: Path, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", path.stem.lower()).strip("-")[:34] or "mail"
    base = base if base[0].isalnum() else "m" + base
    ident, n = base, 1
    while ident in taken:
        n += 1
        ident = f"{base[:34]}-{n}"
    taken.add(ident)
    return ident


def collect(inbox: Path) -> list[tuple[Path, Mail]]:
    """What has arrived in inbox/new, oldest first, at most one run's worth."""
    new = inbox / "new"
    files = sorted((p for p in new.iterdir() if p.is_file() and not p.name.startswith(".")),
                   key=lambda p: (p.stat().st_mtime, p.name)) if new.is_dir() else []
    taken: set[str] = set()
    return [(p, Mail(_mail_id(p, taken), p.read_text(errors="replace"))) for p in files[:MAX_PER_RUN]]


def file_away(inbox: Path, paths: list[Path]) -> None:
    cur = inbox / "cur"
    cur.mkdir(parents=True, exist_ok=True)
    for p in paths:
        p.rename(cur / p.name)


def watch_once(agent: SwitchAgent, quarantine_id: str, inbox: Path, names: Names, *, delay: float = 0.5,
               sleep=time.sleep) -> str | None:
    """Post whatever has arrived as one run. Returns the run id, or None when the inbox was empty."""
    batch = collect(inbox)
    if not batch:
        return None
    run = send(agent, quarantine_id, [m for _, m in batch], names, delay=delay, sleep=sleep)
    file_away(inbox, [p for p, _ in batch])
    return run


def run_watch(inbox: Path, interval: float, settle: float = 2.0, names: Names | None = None) -> None:
    import logging
    log = logging.getLogger("airlock.mailroom")
    names = names or Names.from_env()
    agent = load_agent(names.mailroom)
    quarantine = resolve_room(agent, names.quarantine)
    for sub in ("new", "cur", "tmp"):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
    log.info("mailroom watching %s/new every %ss", inbox, interval)
    while True:
        if any((inbox / "new").iterdir()):
            time.sleep(settle)  # let a burst of deliveries land, so they go as one run
            run = watch_once(agent, quarantine, inbox, names)
            if run:
                log.info("posted run %s", run)
        time.sleep(interval)
