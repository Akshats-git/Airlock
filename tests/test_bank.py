import json

import pytest
from typer.testing import CliRunner

from airlock.bank import ops
from airlock.bank.cli import app
from airlock.erp import ops as erp_ops
from tests.conftest import ACME, GLOBEX, NORTHW


@pytest.fixture
def desk(erp, tmp_path, monkeypatch):
    monkeypatch.setenv("AIRLOCK_BANK", str(tmp_path / "bank.sqlite"))
    monkeypatch.setenv("AIRLOCK_RECEIPTS", str(tmp_path / "inbox" / "receipts.jsonl"))
    for rec in (ACME, GLOBEX, NORTHW):
        erp_ops.match(erp, rec)
    erp_ops.resolve(erp, "I-0003", "short-pay", "sam")
    erp_ops.propose_batch(erp)
    return tmp_path


ACME_IBAN, GLOBEX_IBAN, NORTHW_IBAN = "GB82WEST12345698765432", "DE89370400440532013000", "FR1420041010050500013M02606"
ATTACKER = "GB33BUKB20201555555555"


def run(*args):
    return CliRunner().invoke(app, ["pay", *args])


def acme(amount="12400.00", iban=ACME_IBAN, to="Acme Corp", pid="PAY-0001"):
    return run(pid, "--to", to, "--iban", iban, "--amount", amount)


def test_pay_moves_money_and_writes_receipt(desk):
    r = acme()
    assert r.exit_code == 0
    assert r.output.strip() == "PAID PAY-0001 Acme Corp 12400.00 USD tx=TX-0001 approved_by=switch-approval"
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS - 1240000
    receipt = json.loads((desk / "inbox" / "receipts.jsonl").read_text())
    assert receipt["payment_id"] == "PAY-0001"


def test_pay_is_idempotent(desk):
    acme()
    again = acme()
    assert again.exit_code == 0 and again.output.startswith("ALREADY PAID")
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS - 1240000


@pytest.mark.parametrize("args,why", [
    (("PAY-0001", "--to", "Initech Inc", "--iban", ACME_IBAN, "--amount", "12400.00"), "payee"),
    (("PAY-0001", "--to", "Acme Corp", "--iban", ATTACKER, "--amount", "12400.00"), "not Acme Corp's verified account"),
    (("PAY-0001", "--to", "Acme Corp", "--iban", ACME_IBAN, "--amount", "12400.01"), "amount"),
    (("PAY-0001", "--to", "Acme Corp", "--iban", ACME_IBAN, "--amount", "12400"), "two decimals"),
    (("PAY-0003", "--to", "Northwind Traders", "--iban", NORTHW_IBAN, "--amount", "6600.00"), "amount"),  # billed, not short-paid
    (("PAY-0099", "--to", "Acme Corp", "--iban", ACME_IBAN, "--amount", "1.00"), "not in any batch"),
])
def test_pay_refuses_anything_that_differs_from_erp(desk, args, why):
    r = run(*args)
    assert r.exit_code == 1 and r.output.startswith("REFUSED") and why in r.output
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS


def test_a_clerk_that_rewrites_its_own_erp_still_cannot_redirect_money(erp, tmp_path, monkeypatch):
    """The clerk owns the ERP and may be talked round. Say it changes Acme's account there, then batches:
    the batch now names the attacker's account, and whether treasury types that account or the real one,
    the bank refuses, because it pays only accounts on its own list."""
    monkeypatch.setenv("AIRLOCK_BANK", str(tmp_path / "bank.sqlite"))
    erp.execute("UPDATE vendors SET iban=? WHERE id='V-ACME'", (ATTACKER,))
    erp.commit()
    erp_ops.match(erp, ACME)
    _, doc = erp_ops.propose_batch(erp)
    assert ATTACKER in doc  # the controller would see it on the approval card, too
    assert "not Acme Corp's verified account" in acme(iban=ATTACKER).output
    assert "the batch names account" in acme().output
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS


def test_a_vendor_missing_from_the_beneficiary_list_is_never_paid(desk):
    bank = ops.connect()
    bank.execute("DELETE FROM beneficiaries WHERE vendor_id='V-GLOBEX'")
    bank.commit()
    r = run("PAY-0002", "--to", "Globex Ltd", "--iban", GLOBEX_IBAN, "--amount", "4300.00")
    assert "no verified account on the bank's beneficiary list" in r.output


def test_beneficiary_list_starts_with_the_onboarded_vendors(desk):
    rows = ops.connect().execute("SELECT vendor_id, iban, verified_by FROM beneficiaries ORDER BY vendor_id").fetchall()
    assert ("V-ACME", ACME_IBAN, "vendor onboarding call") in [tuple(r) for r in rows] and len(rows) == 4


def test_per_payment_cap(desk, monkeypatch):
    monkeypatch.setenv("AIRLOCK_PAY_CAP_CENTS", "1000000")
    assert "over the per-payment cap" in acme().output


def test_snapshot_for_ledger_page(desk):
    run("PAY-0002", "--to", "Globex Ltd", "--iban", GLOBEX_IBAN, "--amount", "4300.00")
    snap = ops.snapshot(ops.connect())
    assert snap["balance_cents"] == ops.OPENING_CENTS - 430000
    assert snap["transfers"][0]["payee"] == "Globex Ltd"
