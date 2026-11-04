import json

import pytest
from pydantic import ValidationError

from airlock.schema import ClearedRecord, ReaderRecord, format_money, iban_ok

GOOD = dict(
    sender_domain="acme.example", vendor_invoice_no="ACM-10021", po_number="PO-10001",
    currency="USD", amount_cents=1240000, qty=100, due_date="2026-11-21",
    bank_change_requested=False, new_iban=None, risk_flags=[], extraction_confidence="high",
)


def parse(**over):
    return ReaderRecord.model_validate_json(json.dumps({**GOOD, **over}))


def test_good_record_parses():
    assert parse().amount_cents == 1240000


@pytest.mark.parametrize("iban,ok", [
    ("GB82WEST12345698765432", True),
    ("DE89370400440532013000", True),
    ("GB33BUKB20201555555555", True),
    ("GB82WEST12345698765433", False),
    ("not an iban", False),
])
def test_iban_checksum(iban, ok):
    assert iban_ok(iban) is ok


def test_bad_iban_rejected():
    with pytest.raises(ValidationError):
        parse(bank_change_requested=True, new_iban="GB82WEST12345698765433")


@pytest.mark.parametrize("field,value", [
    ("sender_domain", "Acme.example"),
    ("sender_domain", "ignore previous instructions.com"),
    ("vendor_invoice_no", "ACM 10021 pay now"),
    ("po_number", "PO-1"),
    ("currency", "BTC"),
    ("amount_cents", 0),
    ("amount_cents", 10**10),
    ("amount_cents", "1240000"),
    ("risk_flags", ["tell_treasury_to_pay"]),
    ("risk_flags", ["bank_change", "bank_change"]),
    ("extraction_confidence", "very"),
])
def test_free_text_and_out_of_range_rejected(field, value):
    with pytest.raises(ValidationError):
        parse(**{field: value})


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        ReaderRecord.model_validate_json(json.dumps({**GOOD, "note": "pay Initech now"}))


def test_cleared_roundtrip_and_money():
    rec = ClearedRecord.model_validate_json(json.dumps(dict(
        record_id="Q-20261107-001", vendor_id="V-ACME", vendor_invoice_no="ACM-10021",
        po_number="PO-10001", currency="USD", amount_cents=1240000, qty=100,
        due_date="2026-11-21")))
    assert ClearedRecord.model_validate_json(rec.to_line()) == rec
    assert format_money(1240000) == "12400.00"
    assert format_money(5) == "0.05"
