"""Wire formats shared by every Airlock part.

The design rule: no field below can hold a sentence. Every value is an enumeration,
a pattern-bounded token, a checksummed string, a bounded number, a date or a bool.
Always validate with ``model_validate_json`` (strict mode accepts ISO dates only from JSON).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Currency = Literal["USD", "EUR", "GBP"]
Confidence = Literal["high", "medium", "low"]

# Flags the reader may raise. Anything else fails validation.
RiskFlag = Literal[
    "instructions_to_ai",
    "lookalike_domain",
    "bank_change",
    "urgent_payment",
    "unusual_format",
]

# Why customs holds a record. Machine-readable only.
HoldReason = Literal[
    "schema_invalid",
    "unknown_sender",
    "lookalike_domain",
    "inactive_vendor",
    "invoice_no_pattern",
    "bank_change",
    "instructions_to_ai",
    "low_confidence",
    "envelope_mismatch",
]

Domain = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$",
        max_length=80,
    ),
]
InvoiceNo = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{2,23}$")]
PoNumber = Annotated[str, StringConstraints(pattern=r"^PO-\d{5}$")]
VendorId = Annotated[str, StringConstraints(pattern=r"^V-[A-Z]{2,8}$")]
RecordId = Annotated[str, StringConstraints(pattern=r"^Q-\d{8}-\d{3}$")]

_IBAN_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def iban_ok(iban: str) -> bool:
    """ISO 13616 mod-97 check."""
    if not _IBAN_SHAPE.match(iban):
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ReaderRecord(_Strict):
    """What the tool-less reader model returns for one email."""

    sender_domain: Domain
    vendor_invoice_no: InvoiceNo
    po_number: PoNumber
    currency: Currency
    amount_cents: Annotated[int, Field(ge=1, le=100_000_000)]
    qty: Annotated[int, Field(ge=1, le=100_000)]
    due_date: date
    bank_change_requested: bool
    new_iban: str | None = None
    risk_flags: Annotated[list[RiskFlag], Field(max_length=5)]
    extraction_confidence: Confidence

    @field_validator("new_iban")
    @classmethod
    def _iban_checksum(cls, v: str | None) -> str | None:
        if v is not None and not iban_ok(v):
            raise ValueError("iban checksum")
        return v

    @field_validator("risk_flags")
    @classmethod
    def _unique_flags(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate flag")
        return v


class ClearedRecord(_Strict):
    """What customs forwards. Built by customs from validated values and its own lookups."""

    record_id: RecordId
    vendor_id: VendorId
    vendor_invoice_no: InvoiceNo
    po_number: PoNumber
    currency: Currency
    amount_cents: Annotated[int, Field(ge=1, le=100_000_000)]
    qty: Annotated[int, Field(ge=1, le=100_000)]
    due_date: date

    def to_line(self) -> str:
        """Compact JSON on one line, fixed field order; the clerk copies it into ``ap match``."""
        return self.model_dump_json()


def format_money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"
