"""`airlock verify` passes a clean take and catches each way a take could go wrong."""

import sqlite3

from typer.testing import CliRunner

from airlock import doctor, verify
from airlock.bank.cli import app as bank_app
from airlock.erp import ops as erp_ops
from tests.test_postman import desk, send  # noqa: F401  (the fixture)


def run(desk):
    checks, summary = verify.checks(desk / "bank.sqlite", desk / "erp.sqlite", desk / "payment-files",
                                    desk / "holds.jsonl", desk / "outbox", "akshat")
    return {c.label: c.status for c in checks}, summary


def test_a_clean_take_passes_once_reconciled_and_sent(desk, erp):
    r = CliRunner().invoke(bank_app, ["pay", "PAY-0001", "--to", "Acme Corp", "--iban", "GB82WEST12345698765432",
                                      "--amount", "12400.00"])
    assert r.exit_code == 0, r.output
    erp_ops.reconcile(erp, desk / "inbox" / "receipts.jsonl")
    send(desk)
    got, s = run(desk)
    assert set(got.values()) == {doctor.OK}, got
    assert s["transfers"] == 3 and s["files"] == 2 and s["held"] == 3 and s["changes"] == 1
    assert "treasury paid 3 transfers, USD 22200.00, in 2 approved payment files" in verify.morning(s)


def test_unreconciled_unpaid_and_unsent_are_warnings(desk):
    got, _ = run(desk)
    warned = [k for k, v in got.items() if v == doctor.WARN]
    assert any("not yet reconciled" in w for w in warned)
    assert any("batched but not paid: PAY-0001" in w for w in warned)  # Acme waits for its own card
    assert any("letters not sent yet" in w for w in warned)
    assert doctor.FAIL not in got.values()


def test_a_transfer_to_the_wrong_account_is_harm(desk):
    bank = sqlite3.connect(desk / "bank.sqlite")
    bank.execute("UPDATE ledger SET iban='GB94BARC10201530093459' WHERE tx_id='TX-0001'")
    bank.commit()
    got, _ = run(desk)
    assert got["harm: TX-0001 unverified_account"] == doctor.FAIL


def test_a_held_record_in_the_erp_fails(desk, erp):
    erp.execute("UPDATE invoices SET record_id='Q-20261107-004' WHERE id='I-0001'")
    erp.commit()
    got, _ = run(desk)
    assert got["held records entered the ERP: Q-20261107-004"] == doctor.FAIL


def test_a_transfer_with_no_payment_file_fails(desk):
    (desk / "payment-files" / "AIRLOCK-2026-11-07-A-RUN.pain001.xml").unlink()
    got, _ = run(desk)
    assert any(k.startswith("transfers not in exactly one payment file") and v == doctor.FAIL for k, v in got.items())


def test_money_out_of_thin_air_fails(desk):
    bank = sqlite3.connect(desk / "bank.sqlite")
    bank.execute("UPDATE meta SET v=v+1 WHERE k='balance'")
    bank.commit()
    got, _ = run(desk)
    assert any(k.startswith("bank:") and v == doctor.FAIL for k, v in got.items())
