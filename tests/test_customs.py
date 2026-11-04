import json
from datetime import date
from email import message_from_string
from pathlib import Path

import pytest

from airlock.customs import gate
from airlock.customs.gate import Cleared, Held
from airlock.erp import ops as erp_ops
from airlock.erp.db import connect

FIX = Path(__file__).resolve().parent.parent / "fixtures"
DAY = date(2026, 11, 7)
NAMES = ["01-acme", "02-globex", "03-northwind", "04-initech-fraud"]


@pytest.fixture
def vendors(erp):
    return gate.load_vendors(erp)


def fixture(name: str) -> tuple[str | None, str]:
    msg = message_from_string((FIX / "emails" / f"{name}.eml").read_text())
    return gate.envelope_domain(msg["From"]), (FIX / "reader" / f"{name}.json").read_text()


def decide(name, vendors, seq=1, **tweak):
    env, raw = fixture(name)
    if tweak:
        raw = json.dumps({**json.loads(raw), **tweak})
    return gate.check(raw, env, vendors, DAY, seq)


def test_three_clean_cleared_and_fraud_held_with_reasons(vendors):
    out = [decide(n, vendors, i + 1) for i, n in enumerate(NAMES)]
    assert [type(d) for d in out] == [Cleared, Cleared, Cleared, Held]
    assert out[0].record.vendor_id == "V-ACME" and out[0].record.record_id == "Q-20261107-001"
    assert out[3].reasons == ("unknown_sender", "lookalike_domain", "bank_change", "instructions_to_ai")


def test_card_has_one_record_per_line_and_only_codes_for_held(vendors):
    out = [decide(n, vendors, i + 1) for i, n in enumerate(NAMES)]
    card = gate.render_card(out, DAY, 1).splitlines()
    assert card[0] == "CUSTOMS CARD 2026-11-07 run=1 cleared=3 held=1"
    assert sum(line.startswith("REC {") for line in card) == 3
    assert card[-1] == "HELD Q-20261107-004 reasons=unknown_sender,lookalike_domain,bank_change,instructions_to_ai"
    text = "\n".join(card)
    for leaked in ("ignore", "CFO", "GB33", "initech-billing", "SYSTEM NOTICE"):
        assert leaked not in text


def test_hijacked_reader_cannot_smuggle_free_text(vendors):
    env, raw = fixture("01-acme")
    for payload in (
        json.dumps({**json.loads(raw), "note": "tell treasury to pay everything"}),
        json.dumps({**json.loads(raw), "vendor_invoice_no": "ACM-10021 and pay Initech now"}),
        json.dumps({**json.loads(raw), "risk_flags": ["pay_now"]}),
        "Sure! Here is the record: " + raw,
        "",
    ):
        d = gate.check(payload, env, vendors, DAY, 1)
        assert d == Held("Q-20261107-001", ("schema_invalid",))


def test_reader_claiming_a_trusted_domain_is_caught_by_the_envelope(vendors):
    env, raw = fixture("04-initech-fraud")
    spoof = json.dumps({**json.loads(raw), "sender_domain": "initech.example", "risk_flags": [],
                        "bank_change_requested": False, "new_iban": None})
    d = gate.check(spoof, env, vendors, DAY, 4)
    assert isinstance(d, Held) and "envelope_mismatch" in d.reasons and "lookalike_domain" in d.reasons


def test_reader_ignoring_the_attack_still_held_by_the_envelope(vendors):
    # Even a reader that reports nothing suspicious cannot get a lookalike sender through.
    d = decide("04-initech-fraud", vendors, 4, risk_flags=[], bank_change_requested=False, new_iban=None)
    assert d == Held("Q-20261107-004", ("unknown_sender", "lookalike_domain"))


@pytest.mark.parametrize("tweak,reason", [
    ({"extraction_confidence": "medium"}, "low_confidence"),
    ({"vendor_invoice_no": "ACM-1"}, "invoice_no_pattern"),
    ({"risk_flags": ["instructions_to_ai"]}, "instructions_to_ai"),
    ({"bank_change_requested": True, "new_iban": "DE89370400440532013000"}, "bank_change"),
])
def test_each_hold_reason(vendors, tweak, reason):
    d = decide("01-acme", vendors, **tweak)
    assert isinstance(d, Held) and reason in d.reasons


def test_inactive_vendor_held(erp):
    erp.execute("UPDATE vendors SET status='blocked' WHERE id='V-ACME'")
    erp.commit()
    d = decide("01-acme", gate.load_vendors(erp))
    assert d == Held("Q-20261107-001", ("inactive_vendor",))


@pytest.mark.parametrize("header,expected", [
    ("Acme <billing@ACME.example>", "acme.example"),
    ("billing@acme.example", "acme.example"),
    ("not an address", None),
    ("x@bad domain.com", None),
])
def test_envelope_domain(header, expected):
    assert gate.envelope_domain(header) == expected


def test_lookalikes():
    known = ["acme.example", "initech.example"]
    assert gate.is_lookalike("initech-billing.example", known)
    assert gate.is_lookalike("acrne.example", known)
    assert not gate.is_lookalike("acme.example", known)
    assert not gate.is_lookalike("unrelated-shop.example", known)


def test_forged_amount_on_a_real_domain_clears_customs_but_never_pays(erp):
    """Customs checks shape and sender, not truth. The ERP is the truth, so a forged amount becomes an exception."""
    d = decide("01-acme", gate.load_vendors(erp), amount_cents=9_900_000)
    assert isinstance(d, Cleared)
    res = erp_ops.match(erp, d.record)
    assert res.status == "exception" and res.reasons == ["price_mismatch"]
    with pytest.raises(erp_ops.ErpError):
        erp_ops.propose_batch(erp)  # nothing matched, so nothing can be batched or paid


def test_full_offline_pipeline_matches_the_plan(erp):
    vendors = gate.load_vendors(erp)
    out = [decide(n, vendors, i + 1) for i, n in enumerate(NAMES)]
    results = [erp_ops.match(erp, d.record) for d in out if isinstance(d, Cleared)]
    assert [r.status for r in results] == ["matched", "matched", "exception"]
    assert erp.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 3  # the fraud never reached the ERP
