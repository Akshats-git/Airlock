import json

import pytest
from typer.testing import CliRunner

from airlock.bank import ops
from airlock.bank.cli import app
from airlock.erp import ops as erp_ops
from airlock.erp.db import connect as erp_connect, erp_path
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


def run(*args):
    return CliRunner().invoke(app, ["pay", *args])


def test_pay_moves_money_and_writes_receipt(desk):
    r = run("PAY-0001", "--to", "Acme Corp", "--amount", "12400.00")
    assert r.exit_code == 0
    assert r.output.strip() == "PAID PAY-0001 Acme Corp 12400.00 USD tx=TX-0001 approved_by=switch-approval"
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS - 1240000
    receipt = json.loads((desk / "inbox" / "receipts.jsonl").read_text())
    assert receipt["payment_id"] == "PAY-0001"


def test_pay_is_idempotent(desk):
    run("PAY-0001", "--to", "Acme Corp", "--amount", "12400.00")
    again = run("PAY-0001", "--to", "Acme Corp", "--amount", "12400.00")
    assert again.exit_code == 0 and again.output.startswith("ALREADY PAID")
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS - 1240000


@pytest.mark.parametrize("args,why", [
    (("PAY-0001", "--to", "Initech Inc", "--amount", "12400.00"), "payee"),
    (("PAY-0001", "--to", "Acme Corp", "--amount", "12400.01"), "amount"),
    (("PAY-0001", "--to", "Acme Corp", "--amount", "12400"), "two decimals"),
    (("PAY-0003", "--to", "Northwind Traders", "--amount", "6600.00"), "amount"),  # billed, not short-paid
    (("PAY-0099", "--to", "Acme Corp", "--amount", "1.00"), "not in any batch"),
])
def test_pay_refuses_anything_that_differs_from_erp(desk, args, why):
    r = run(*args)
    assert r.exit_code == 1 and r.output.startswith("REFUSED") and why in r.output
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS


def test_pay_refuses_changed_account_and_unverified_vendor(desk):
    erp = erp_connect(erp_path())
    erp.execute("UPDATE payments SET payee_iban='GB33BUKB20201555555555' WHERE id='PAY-0001'")
    erp.commit()
    assert "differs from the verified account" in run("PAY-0001", "--to", "Acme Corp", "--amount", "12400.00").output
    erp.execute("UPDATE vendors SET iban_verified=0 WHERE id='V-GLOBEX'")
    erp.commit()
    assert "no verified bank account" in run("PAY-0002", "--to", "Globex Ltd", "--amount", "4300.00").output


def test_per_payment_cap(desk, monkeypatch):
    monkeypatch.setenv("AIRLOCK_PAY_CAP_CENTS", "1000000")
    assert "over the per-payment cap" in run("PAY-0001", "--to", "Acme Corp", "--amount", "12400.00").output


def test_snapshot_for_ledger_page(desk):
    run("PAY-0002", "--to", "Globex Ltd", "--amount", "4300.00")
    snap = ops.snapshot(ops.connect())
    assert snap["balance_cents"] == ops.OPENING_CENTS - 430000
    assert snap["transfers"][0]["payee"] == "Globex Ltd"
