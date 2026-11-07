import json

import pytest
from typer.testing import CliRunner

from airlock.bank import ops
from airlock.erp.seed import VENDORS
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
    assert r.output.splitlines()[0] == "PAID PAY-0001 Acme Corp 12400.00 USD tx=TX-0001 approved_by=switch-approval"
    assert r.output.splitlines()[1].startswith("FILE payment-files/AIRLOCK-PAY-0001.pain001.xml: ISO 20022")
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS - 1240000
    receipt = json.loads((desk / "inbox" / "receipts.jsonl").read_text())
    assert receipt["payment_id"] == "PAY-0001"


def test_the_receipt_names_the_controller_when_the_bank_knows_them(desk, monkeypatch):
    monkeypatch.setenv("AIRLOCK_CONTROLLER", "akshat")
    r = acme()
    assert r.output.splitlines()[0].endswith("approved_by=akshat")
    assert json.loads((desk / "inbox" / "receipts.jsonl").read_text())["approved_by"] == "akshat"
    monkeypatch.setenv("AIRLOCK_APPROVER", "someone-else")  # an explicit override still wins
    assert run("PAY-0002", "--to", "Globex Ltd", "--iban", GLOBEX_IBAN, "--amount", "4300.00").output.splitlines()[0].endswith("approved_by=someone-else")


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
    assert ("V-ACME", ACME_IBAN, "vendor onboarding call") in [tuple(r) for r in rows] and len(rows) == len(VENDORS)


def test_per_payment_cap(desk, monkeypatch):
    monkeypatch.setenv("AIRLOCK_PAY_CAP_CENTS", "1000000")
    assert "over the per-payment cap" in acme().output


def test_snapshot_for_ledger_page(desk):
    run("PAY-0002", "--to", "Globex Ltd", "--iban", GLOBEX_IBAN, "--amount", "4300.00")
    snap = ops.snapshot(ops.connect())
    assert snap["balance_cents"] == ops.OPENING_CENTS - 430000
    assert snap["transfers"][0]["payee"] == "Globex Ltd"


# -- changing a beneficiary's account -----------------------------------------------------------------------
INITECH_PHONE = "+1-555-0104"


def change(*args):
    return CliRunner().invoke(app, ["change", "apply", *args])


def test_a_callback_must_use_the_number_the_bank_holds(desk):
    bank = ops.connect()
    with pytest.raises(ops.BankError, match=r"\+1-555-0199 is not the number on file for Initech Inc; call \+1-555-0104"):
        ops.record_verification(bank, "V-INITECH", ATTACKER, "+1-555-0199", "sam")
    with pytest.raises(ops.BankError, match="checksum"):
        ops.record_verification(bank, "V-INITECH", "GB00BUKB20201555555555", INITECH_PHONE, "sam")
    with pytest.raises(ops.BankError, match="already"):
        ops.record_verification(bank, "V-INITECH", "NL91ABNA0417164300", INITECH_PHONE, "sam")
    with pytest.raises(ops.BankError, match="not on the bank's beneficiary list"):
        ops.record_verification(bank, "V-UMB", ATTACKER, INITECH_PHONE, "sam")
    line = ops.record_verification(bank, "V-INITECH", ATTACKER, INITECH_PHONE, "sam")
    assert line.startswith(f"BANK DETAILS VERIFIED BC-0001 Initech Inc NL91ABNA0417164300 -> {ATTACKER} called "
                           f"{INITECH_PHONE} (the number on file) by sam. Not applied")
    assert ops.beneficiary(bank, "V-INITECH")["iban"] == "NL91ABNA0417164300"  # recorded, not applied


def test_nothing_changes_without_a_recorded_callback(desk):
    r = change("BC-0001", "--to", "Initech Inc", "--iban", ATTACKER)
    assert r.exit_code == 1 and "needs the AP specialist's callback first" in r.output


def test_apply_takes_exactly_what_was_verified(desk, erp):
    ops.record_verification(ops.connect(), "V-INITECH", ATTACKER, INITECH_PHONE, "sam")
    assert "not 'Acme Corp'" in change("BC-0001", "--to", "Acme Corp", "--iban", ATTACKER).output
    assert f"verified {ATTACKER}, not GB94BARC10201530093459" in change(
        "BC-0001", "--to", "Initech Inc", "--iban", "GB94BARC10201530093459").output
    r = change("BC-0001", "--to", "Initech Inc", "--iban", ATTACKER)
    assert r.exit_code == 0 and r.output.startswith(f"APPLIED BC-0001 Initech Inc now pays to {ATTACKER}")
    b = ops.beneficiary(ops.connect(), "V-INITECH")
    assert b["iban"] == ATTACKER and "sam, callback +1-555-0104; BC-0001" == b["verified_by"]
    assert erp.execute("SELECT iban FROM vendors WHERE id='V-INITECH'").fetchone()[0] == ATTACKER  # the copy follows
    assert "beneficiary_change" in erp_ops.decision_log(erp)
    assert "is applied, not pending" in change("BC-0001", "--to", "Initech Inc", "--iban", ATTACKER).output


def test_the_controller_cannot_verify_their_own_change(desk, monkeypatch):
    monkeypatch.setenv("AIRLOCK_CONTROLLER", "akshat")
    ops.record_verification(ops.connect(), "V-INITECH", ATTACKER, INITECH_PHONE, "Akshat")
    r = change("BC-0001", "--to", "Initech Inc", "--iban", ATTACKER)
    assert r.exit_code == 1 and "the controller; a second person must call" in r.output


def test_a_newer_callback_replaces_an_older_one(desk):
    bank = ops.connect()
    ops.record_verification(bank, "V-INITECH", ATTACKER, INITECH_PHONE, "sam")
    ops.record_verification(bank, "V-INITECH", "GB94BARC10201530093459", INITECH_PHONE, "sam")
    assert "is superseded" in change("BC-0001", "--to", "Initech Inc", "--iban", ATTACKER).output


# -- one approval for the routine payment run ----------------------------------------------------------------
import re as _re  # noqa: E402



def run_args(doc):
    m = _re.search(r"^RUN \./bin/bank pay-run (\S+) --count (\d+) --total (\S+) --digest (\S+)$", doc, _re.M)
    return list(m.groups())


@pytest.fixture
def batch(erp, tmp_path, monkeypatch):
    monkeypatch.setenv("AIRLOCK_BANK", str(tmp_path / "bank.sqlite"))
    monkeypatch.setenv("AIRLOCK_RECEIPTS", str(tmp_path / "inbox" / "receipts.jsonl"))
    for rec in (ACME, GLOBEX, NORTHW):
        erp_ops.match(erp, rec)
    erp_ops.resolve(erp, "I-0003", "short-pay", "sam")
    return erp_ops.propose_batch(erp)[1]


def pay_run(*args):
    return CliRunner().invoke(app, ["pay-run", args[0], "--count", args[1], "--total", args[2], "--digest", args[3]])


def test_routine_payments_go_on_one_card_and_large_ones_on_their_own(batch):
    assert run_args(batch)[:3] == ["2026-11-07-A", "2", "9800.00"]
    own = batch.split("## Own approval each")[1]
    assert "PAY-0001 | Acme Corp | ACM-10021 | GB82WEST12345698765432 | 12400.00 USD | over 10000.00" in own
    assert "PAY-0001" not in batch.split("## Own approval each")[0]


def test_the_run_pays_exactly_its_payments_and_nothing_else(batch, tmp_path):
    r = pay_run(*run_args(batch))
    assert r.exit_code == 0, r.output
    lines = r.output.strip().splitlines()
    assert [ln.split()[1] for ln in lines[:-2]] == ["PAY-0002", "PAY-0003"]
    assert lines[-2] == "RUN 2026-11-07-A: 2 routine payments, 9800.00 USD, approved_by=switch-approval"
    assert lines[-1] == ("FILE payment-files/AIRLOCK-2026-11-07-A-RUN.pain001.xml: ISO 20022 credit transfer, "
                         "2 payments, 9800.00; bank status ACSC")
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS - 980000  # Acme's 12,400 waits for its own card
    assert len((tmp_path / "inbox" / "receipts.jsonl").read_text().splitlines()) == 2
    assert acme().exit_code == 0  # its own card, its own command


@pytest.mark.parametrize("field, value", [(1, "3"), (2, "9800.01"), (3, "0000000000")])
def test_a_run_that_differs_from_the_card_pays_nothing(batch, field, value):
    args = run_args(batch)
    args[field] = value
    r = pay_run(*args)
    assert r.exit_code == 1 and "doesn't match the bank's own view" in r.output and "nothing was paid" in r.output
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS


def test_a_rewritten_erp_account_drops_out_of_the_run_and_breaks_it(batch, erp):
    erp.execute("UPDATE payments SET payee_iban=? WHERE id='PAY-0002'", (ATTACKER,))
    erp.commit()
    r = pay_run(*run_args(batch))
    assert r.exit_code == 1 and "1 routine payments" in r.output
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS


def test_a_recently_changed_account_needs_its_own_card(erp, tmp_path, monkeypatch):
    monkeypatch.setenv("AIRLOCK_BANK", str(tmp_path / "bank.sqlite"))
    bank = ops.connect()
    ops.record_verification(bank, "V-GLOBEX", "GB94BARC10201530093459", "+44-20-5550-102", "sam")
    ops.apply_change(bank, erp, "BC-0001", "Globex Ltd", "GB94BARC10201530093459", "switch-approval")
    erp_ops.match(erp, GLOBEX)
    doc = erp_ops.propose_batch(erp)[1]
    assert "RUN " not in doc and "| account changed on" in doc
