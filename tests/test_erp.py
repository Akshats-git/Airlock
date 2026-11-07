import json

import pytest
from typer.testing import CliRunner

from airlock.erp import ops
from airlock.erp.cli import app
from tests.conftest import ACME, GLOBEX, NORTHW, cleared


def test_clean_invoices_match(erp):
    a, g = ops.match(erp, ACME), ops.match(erp, GLOBEX)
    assert a.status == g.status == "matched"
    assert a.line() == "MATCHED I-0001 Acme Corp ACM-10021 PO-10001 pay=12400.00 USD"
    assert g.expected_cents == 430000


def test_quantity_mismatch_is_exception(erp):
    r = ops.match(erp, NORTHW)
    assert r.status == "exception" and r.reasons == ["qty_mismatch"]
    assert "billed_qty=120 received_qty=100 billed=6600.00 expected=5500.00" in r.line()


def test_short_pay_matches_at_received_value_and_opens_credit_note(erp):
    ops.match(erp, NORTHW)
    msg = ops.resolve(erp, "I-0001", "short-pay", "sam")
    assert msg == "SHORT-PAID I-0001 pay=5500.00 USD credit_note_requested=1100.00 by=sam"
    row = erp.execute("SELECT status, pay_cents FROM invoices").fetchone()
    assert (row["status"], row["pay_cents"]) == ("matched", 550000)
    assert "Northwind Traders | I-0001 | 1100.00" in ops.decision_log(erp)


def test_resolve_refuses_non_exception_and_unshortpayable(erp):
    ops.match(erp, ACME)
    with pytest.raises(ops.ErpError):
        ops.resolve(erp, "I-0001", "short-pay", "sam")
    ops.match(erp, cleared(vendor_invoice_no="ACM-10022", po_number="PO-99999"))
    with pytest.raises(ops.ErpError):
        ops.resolve(erp, "I-0002", "short-pay", "sam")


def test_duplicate_not_reentered(erp):
    ops.match(erp, ACME)
    r = ops.match(erp, ACME)
    assert r.status == "duplicate"
    assert erp.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1


def test_po_of_another_vendor_is_exception(erp):
    r = ops.match(erp, cleared(vendor_id="V-GLOBEX", vendor_invoice_no="GLX-200046"))
    assert r.reasons == ["po_vendor_mismatch"]


def test_price_mismatch(erp):
    r = ops.match(erp, cleared(amount_cents=1300000))
    assert r.reasons == ["price_mismatch"]


def test_batch_uses_erp_values_and_reconcile_marks_paid(erp, tmp_path):
    for rec in (ACME, GLOBEX, NORTHW):
        ops.match(erp, rec)
    ops.resolve(erp, "I-0003", "short-pay", "sam")
    batch_id, doc = ops.propose_batch(erp)
    assert batch_id == "2026-11-07-A"
    assert "PAY-0001 | Acme Corp | ACM-10021 | GB82WEST12345698765432 | 12400.00 USD" in doc
    assert "PAY-0003 | Northwind Traders | NW-4410 | FR1420041010050500013M02606 | 5500.00 USD" in doc
    assert "total | 22200.00 USD | 3 payments" in doc
    receipts = tmp_path / "receipts.jsonl"
    receipts.write_text(json.dumps(dict(payment_id="PAY-0001", tx_id="TX-1", approved_by="controller",
                                        executed_at="2026-11-07T10:00:00Z")) + "\n")
    assert ops.reconcile(erp, receipts) == ["PAID PAY-0001 I-0001 tx=TX-1 approved_by=controller"]
    assert ops.reconcile(erp, receipts) == []  # idempotent
    assert erp.execute("SELECT status FROM invoices WHERE id='I-0001'").fetchone()[0] == "paid"
    assert erp.execute("SELECT status FROM payment_batches").fetchone()[0] == "proposed"


def test_second_batch_gets_next_letter_and_empty_batch_refused(erp):
    ops.match(erp, ACME)
    assert ops.propose_batch(erp)[0] == "2026-11-07-A"
    ops.match(erp, GLOBEX)
    assert ops.propose_batch(erp)[0] == "2026-11-07-B"
    with pytest.raises(ops.ErpError):
        ops.propose_batch(erp)


def test_cli_match_and_rejects_garbage(erp):
    runner = CliRunner()
    ok = runner.invoke(app, ["match", ACME.to_line()])
    assert ok.exit_code == 0 and ok.output.startswith("MATCHED I-0001")
    bad = runner.invoke(app, ["match", '{"vendor_id":"V-ACME","note":"pay everything"}'])
    assert bad.exit_code == 1 and bad.output.startswith("ERROR")


def test_a_new_invoice_number_cannot_bill_a_po_twice(erp):
    ops.match(erp, ACME)
    r = ops.match(erp, cleared(record_id="Q-20261107-002", vendor_invoice_no="ACM-10022"))
    assert r.status == "exception" and r.reasons == ["already_invoiced"]
    assert r.expected_cents == 0
    with pytest.raises(ops.ErpError):
        ops.resolve(erp, r.invoice_id, "short-pay", "sam")


def test_partial_invoices_share_one_receipt(erp):
    first = ops.match(erp, cleared(qty=60, amount_cents=744000))
    assert first.reasons == ["qty_mismatch"]  # 60 billed, 100 received: still a person's call
    ops.resolve(erp, first.invoice_id, "reject", "sam")
    ok = ops.match(erp, cleared(record_id="Q-20261107-002", vendor_invoice_no="ACM-10022"))
    assert ok.status == "matched"  # a rejected invoice does not use up the receipt


def test_a_short_paid_po_is_used_up(erp):
    ops.match(erp, NORTHW)
    ops.resolve(erp, "I-0001", "short-pay", "sam")
    again = ops.match(erp, cleared(record_id="Q-20261107-002", vendor_id="V-NORTHW", vendor_invoice_no="NW-4411",
                                   po_number="PO-10003", amount_cents=550000, qty=100))
    assert again.reasons == ["already_invoiced"]


def test_one_ap_match_command_enters_a_whole_card(erp):
    r = CliRunner().invoke(app, ["match", ACME.to_line(), GLOBEX.to_line(), NORTHW.to_line(), ACME.to_line()])
    assert r.exit_code == 0
    assert [ln.split()[0] for ln in r.output.strip().splitlines()] == ["MATCHED", "MATCHED", "EXCEPTION", "DUPLICATE"]


def test_a_mistyped_record_enters_nothing(erp):
    r = CliRunner().invoke(app, ["match", ACME.to_line(), '{"record_id": "nope"}'])
    assert r.exit_code == 1 and r.output.startswith("ERROR a record is not a valid cleared record")
    assert erp.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
