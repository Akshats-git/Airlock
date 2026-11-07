"""Customs: the deterministic schema gate between quarantine and the AP desk.

Plain code, so it cannot be talked round. It never copies text from the reader or the email:
the only things it emits are values it validated, ids it assigned, and fixed reason codes.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from email.utils import parseaddr
from typing import get_args

from pydantic import ValidationError

from airlock.schema import ClearedRecord, Domain, HoldReason, ReaderRecord

_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
# Reasons are always reported in this fixed order, whatever order they were found in.
_ORDER: tuple[str, ...] = get_args(HoldReason)


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    domain: str
    invoice_pattern: str
    active: bool


@dataclass(frozen=True)
class Cleared:
    record: ClearedRecord


@dataclass(frozen=True)
class Held:
    record_id: str
    reasons: tuple[str, ...]
    vendor_id: str | None = field(default=None, compare=False)  # set only when the envelope is a known vendor's


Decision = Cleared | Held


def load_vendors(erp: sqlite3.Connection) -> list[Vendor]:
    """The vendor master, read-only. Customs looks vendors up itself and trusts nothing the reader says."""
    return [Vendor(r["id"], r["name"], r["email_domain"], r["invoice_no_pattern"], r["status"] == "active")
            for r in erp.execute("SELECT * FROM vendors")]


def envelope_domain(from_header: str) -> str | None:
    """Domain of the From address as mailroom saw it; None if it is not a plain domain."""
    _, addr = parseaddr(from_header)
    if addr.lower() not in from_header.lower():  # parseaddr silently repairs odd input; we don't
        return None
    domain = addr.rpartition("@")[2].lower()
    return domain if _DOMAIN.match(domain) and len(domain) <= 80 else None


def is_lookalike(domain: str, known: list[str]) -> bool:
    """A domain that is not a known vendor's but resembles one (shared name, small edit distance)."""
    if domain in known:
        return False
    label = domain.split(".")[0]
    for k in known:
        k_label = k.split(".")[0]
        if len(k_label) >= 4 and k_label in label:
            return True
        if SequenceMatcher(None, domain, k).ratio() >= 0.85:
            return True
    return False


def record_id_for(day: date, seq: int) -> str:
    return f"Q-{day:%Y%m%d}-{seq:03d}"


def check(reader_json: str, envelope: str | None, vendors: list[Vendor], day: date, seq: int) -> Decision:
    """Decide one record. ``envelope`` is the sender domain from the mail headers, not from the reader."""
    rid = record_id_for(day, seq)
    try:
        rec = ReaderRecord.model_validate_json(reader_json)
    except ValidationError:
        return Held(rid, ("schema_invalid",))

    reasons: set[str] = set()
    known = [v.domain for v in vendors]
    vendor = next((v for v in vendors if v.domain == envelope), None) if envelope else None

    if envelope is None or rec.sender_domain != envelope:
        reasons.add("envelope_mismatch")
    if vendor is None:
        reasons.add("unknown_sender")
        if any(is_lookalike(d, known) for d in {rec.sender_domain, envelope} if d):
            reasons.add("lookalike_domain")
    else:
        if not vendor.active:
            reasons.add("inactive_vendor")
        if not re.fullmatch(vendor.invoice_pattern, rec.vendor_invoice_no):
            reasons.add("invoice_no_pattern")
    if rec.bank_change_requested or rec.new_iban is not None or "bank_change" in rec.risk_flags:
        reasons.add("bank_change")
    if "instructions_to_ai" in rec.risk_flags:
        reasons.add("instructions_to_ai")
    if "lookalike_domain" in rec.risk_flags:
        reasons.add("lookalike_domain")
    if rec.extraction_confidence != "high":
        reasons.add("low_confidence")

    if reasons or vendor is None:
        return Held(rid, tuple(r for r in _ORDER if r in reasons), vendor.id if vendor else None)
    return Cleared(ClearedRecord(
        record_id=rid, vendor_id=vendor.id, vendor_invoice_no=rec.vendor_invoice_no,
        po_number=rec.po_number, currency=rec.currency, amount_cents=rec.amount_cents,
        qty=rec.qty, due_date=rec.due_date))


def render_card(decisions: list[Decision], day: date, run: int) -> str:
    """One card per intake run, so the clerk takes one turn rather than one per invoice."""
    cleared = [d.record for d in decisions if isinstance(d, Cleared)]
    held = [d for d in decisions if isinstance(d, Held)]
    lines = [f"CUSTOMS CARD {day.isoformat()} run={run} cleared={len(cleared)} held={len(held)}"]
    if cleared:
        lines.append("Run `./bin/ap match '<json>' '<json>' ...` once, with every REC line's JSON in order, and post "
                     "each result line unchanged.")
    lines += [f"REC {c.to_line()}" for c in cleared]
    lines += [f"HELD {h.record_id} reasons={','.join(h.reasons)}" for h in held]
    return "\n".join(lines)


__all__ = ["Cleared", "Held", "Decision", "Vendor", "check", "envelope_domain", "is_lookalike",
           "load_vendors", "render_card", "Domain"]
