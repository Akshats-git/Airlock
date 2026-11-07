"""Which payments a person approves together, and which one at a time.

A controller asked to approve forty cards a morning stops reading them, and an approval nobody reads is no
approval. So routine payments (small, to an account that hasn't changed lately) are approved together as one
payment run, whose card shows how many, how much in total, and a fingerprint of exactly which payments. Every
other payment keeps its own card, with its account on it. The clerk's ERP lays the batch out by this rule; the
bank applies it again from its own records and refuses a run that doesn't match.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date

RECENT_CHANGE_DAYS = 30


def routine_cap_cents() -> int:
    return int(os.environ.get("AIRLOCK_ROUTINE_CAP_CENTS", "1000000"))  # USD 10,000.00


def own_card_reason(cents: int, account_verified_on: str, today: date) -> str | None:
    """None when the payment can ride in the run; otherwise why it needs its own approval."""
    from airlock.schema import format_money

    cap = routine_cap_cents()
    if cents > cap:
        return f"over {format_money(cap)}"
    if (today - date.fromisoformat(account_verified_on[:10])).days < RECENT_CHANGE_DAYS:
        return f"account changed on {account_verified_on[:10]}"
    return None


def digest(lines: list[tuple[str, str, str, int]]) -> str:
    """A fingerprint of (payment id, vendor id, account, cents) for every payment in a run, in any order."""
    text = "\n".join("|".join(map(str, line)) for line in sorted(lines))
    return hashlib.sha256(text.encode()).hexdigest()[:10]
