"""The only message shapes the quarantine agents accept from each other.

Every line a protocol agent acts on is built and parsed here. Parsers use ``fullmatch`` on
the header, so vendor text can never be mistaken for a header: the mailroom always puts its own
header line first, and the reader's reply is a header plus one JSON object.

    mailroom -> customs   @customs ENVELOPE run=090314 n=4 mail=01-acme domain=acme.example
    mailroom -> reader    @reader MAIL run=090314 mail=01-acme  (raw email on the lines below)
    reader   -> customs   @customs READ run=090314 mail=01-acme  (one JSON object below)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_EMAIL_CHARS = 6000  # a chat message has a size cap; a longer email is cut and says so
MAX_READER_CHARS = 2000  # a real record is ~300 chars; anything longer is not a record

_RUN = r"(?P<run>[0-9]{6})"
_MAIL = r"(?P<mail>[a-z0-9][a-z0-9-]{0,39})"
_DOMAIN = r"(?P<domain>none|[a-z0-9][a-z0-9.-]{0,78}[a-z0-9])"

_ENVELOPE = re.compile(rf"(?:@(?P<to>[a-z0-9._-]+) )?ENVELOPE run={_RUN} n=(?P<n>[1-9][0-9]?) mail={_MAIL} domain={_DOMAIN}")
_MAIL_HEAD = re.compile(rf"(?:@(?P<to>[a-z0-9._-]+) )?MAIL run={_RUN} mail={_MAIL}")
_READ_HEAD = re.compile(rf"(?:@(?P<to>[a-z0-9._-]+) )?READ run={_RUN} mail={_MAIL}")
# The AP specialist's record of a bank-detail callback, posted in the desk: VERIFIED V-INITECH <IBAN> called <phone>
_VERIFIED = re.compile(r"VERIFIED (?P<vendor>V-[A-Z]{2,8}) (?P<iban>[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}) called "
                       r"(?P<phone>\+[0-9][0-9-]{5,19})")
# A person on a messaging platform, as Switch names them: @switch-mattermost-<bridge id>-<username>:<server>.
# Agents are @switch-agent-<hex>:<server>, so no agent can ever match.
_PERSON = re.compile(r"@switch-(?!agent-)[a-z]+-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-"
                     r"(?P<user>[a-z0-9._-]+):[A-Za-z0-9.-]+")
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass(frozen=True)
class Envelope:
    run: str
    mail: str
    domain: str | None  # None when the From header was not a plain domain
    total: int  # how many mails the run holds


@dataclass(frozen=True)
class MailJob:
    run: str
    mail: str
    email: str


@dataclass(frozen=True)
class ReadResult:
    run: str
    mail: str
    payload: str  # whatever the reader produced; only customs' schema decides what it means


def valid_mail_id(value: str) -> bool:
    return re.fullmatch(_MAIL, value) is not None


def valid_run_id(value: str) -> bool:
    return re.fullmatch(_RUN, value) is not None


def format_envelope(to: str, run: str, total: int, mail: str, domain: str | None) -> str:
    return f"@{to} ENVELOPE run={run} n={total} mail={mail} domain={domain or 'none'}"


def format_mail(to: str, run: str, mail: str, email: str) -> str:
    body = email.strip("\n")
    if len(body) > MAX_EMAIL_CHARS:
        body = body[:MAX_EMAIL_CHARS] + "\n[TRUNCATED BY MAILROOM]"
    return f"@{to} MAIL run={run} mail={mail}\n-----\n{body}"


def format_read(to: str, run: str, mail: str, payload: str) -> str:
    return f"@{to} READ run={run} mail={mail}\n{payload}"


def parse_envelope(body: str) -> Envelope | None:
    m = _ENVELOPE.fullmatch(body.strip())
    if not m:
        return None
    return Envelope(m["run"], m["mail"], None if m["domain"] == "none" else m["domain"], int(m["n"]))


def parse_mail(body: str) -> MailJob | None:
    head, sep, rest = body.partition("\n")
    m = _MAIL_HEAD.fullmatch(head.strip())
    if not m or not sep:
        return None
    marker, _, email = rest.partition("\n")
    if marker.strip() != "-----":
        return None
    return MailJob(m["run"], m["mail"], email)


def parse_read(body: str) -> ReadResult | None:
    head, _, rest = body.partition("\n")
    m = _READ_HEAD.fullmatch(head.strip())
    if not m:
        return None
    payload = rest.strip()
    fenced = _FENCE.match(payload)
    if fenced:
        payload = fenced.group(1)
    return ReadResult(m["run"], m["mail"], payload)


def clamp_reader_output(text: str) -> str:
    """One compact line, never longer than a record could be. Too long means not a record."""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= MAX_READER_CHARS else one_line[:MAX_READER_CHARS]


@dataclass(frozen=True)
class Verified:
    vendor: str
    iban: str
    phone: str


def parse_verified(body: str) -> Verified | None:
    m = _VERIFIED.fullmatch(body.strip())
    return Verified(m["vendor"], m["iban"], m["phone"]) if m else None


def person_username(sender: str) -> str | None:
    """The chat username of a person, from Switch's own sender id; None for an agent or anything else."""
    m = _PERSON.fullmatch(sender)
    return m["user"] if m else None
