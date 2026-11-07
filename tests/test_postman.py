"""The postman: every letter comes from the records that own the fact, goes to the address the bank holds, and
is sent once."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from airlock import postman
from airlock.agents.customs import Card, record_holds
from airlock.bank import ops as bank_ops
from airlock.bank.cli import app as bank_app
from airlock.customs import gate
from airlock.erp import ops as erp_ops
from tests.conftest import ACME, GLOBEX, NORTHW

INITECH_NEW = "GB33BUKB20201555555555"


@pytest.fixture
def desk(erp, tmp_path, monkeypatch):
    """A batch paid (Northwind short-paid), and Initech's bank details changed after a callback."""
    monkeypatch.setenv("AIRLOCK_BANK", str(tmp_path / "bank.sqlite"))
    monkeypatch.setenv("AIRLOCK_RECEIPTS", str(tmp_path / "inbox" / "receipts.jsonl"))
    for rec in (ACME, GLOBEX, NORTHW):
        erp_ops.match(erp, rec)
    erp_ops.resolve(erp, "I-0003", "short-pay", "sam")
    doc = erp_ops.propose_batch(erp)[1]
    run = next(ln for ln in doc.splitlines() if ln.startswith("RUN ")).split()[2:]
    assert CliRunner().invoke(bank_app, run).exit_code == 0
    bank = bank_ops.connect()
    bank_ops.record_verification(bank, "V-INITECH", INITECH_NEW, "+1-555-0104", "sam")
    bank_ops.apply_change(bank, None, "BC-0001", "Initech Inc", INITECH_NEW, "switch-approval")
    holds = tmp_path / "holds.jsonl"
    record_holds(Card("100000", "", 0, 3, (
        gate.Held("Q-20261107-004", ("bank_change",), "V-FULCRUM"),
        gate.Held("Q-20261107-005", ("unknown_sender", "lookalike_domain", "bank_change")),  # not a vendor
        gate.Held("Q-20261107-006", ("instructions_to_ai",), "V-COBALT"),  # held, but no bank request
    )), holds)
    return tmp_path


def send(desk, smtp=None):
    return postman.run_once(desk / "bank.sqlite", desk / "erp.sqlite", desk / "holds.jsonl", desk / "outbox", smtp)


def test_the_letters_the_records_call_for(desk):
    lines = send(desk)
    assert lines == [
        "SENT bank-change-request-Q-20261107-004 to accounts@fulcrum.example: We received a request to change your bank details",
        "SENT credit-note-I-0003 to accounts@northwind.example: Credit note request: invoice NW-4410",
        "SENT remittance-TX-0001 to accounts@globex.example: Remittance advice: invoice GLX-200045",
        "SENT remittance-TX-0002 to accounts@northwind.example: Remittance advice: invoice NW-4410",
        "SENT bank-change-applied-BC-0001 to accounts@initech.example: Your bank details with us were changed",
    ]
    assert send(desk) == []  # once each
    assert len(list((desk / "outbox").glob("*.eml"))) == 5


def test_letters_say_what_the_records_say(desk):
    send(desk)
    read = lambda key: (desk / "outbox" / f"{key}.eml").read_text()
    cn = read("credit-note-I-0003")
    assert "billed 6600.00 USD for 120 units" in cn and "paying 5500.00 USD" in cn and "credit note for 1100.00 USD" in cn
    ra = read("remittance-TX-0001")
    assert "4300.00 USD" in ra and "account ending 3000" in ra and "PAY-0002, bank transaction TX-0001" in ra
    bc = read("bank-change-request-Q-20261107-004")
    assert "We have not changed anything" in bc and "+44-20-5550-109" in bc and "account ending 1296" in bc
    ap = read("bank-change-applied-BC-0001")
    assert "from the account ending 4300 to the account ending 5555" in ap and "+1-555-0104" in ap


def test_the_address_is_the_one_the_bank_holds_whatever_the_erp_says(desk, erp):
    erp.execute("UPDATE vendors SET email_domain='attacker.example' WHERE id='V-GLOBEX'")
    erp.commit()
    send(desk)
    assert "To: accounts@globex.example" in (desk / "outbox" / "remittance-TX-0001.eml").read_text()


def test_holds_record_ids_and_codes_only(desk):
    rows = [json.loads(x) for x in (desk / "holds.jsonl").read_text().splitlines()]
    assert set(rows[0]) == {"record_id", "vendor_id", "reasons", "run"}


def test_smtp_gets_the_same_letters(desk, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("localhost", 1025)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def send_message(self, msg):
            sent.append(msg["To"])

    monkeypatch.setattr(postman.smtplib, "SMTP", FakeSMTP)
    send(desk, "localhost:1025")
    assert len(sent) == 5 and sent[0] == "accounts@fulcrum.example"


def test_a_stopped_mail_server_leaves_the_letter_due(desk, monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(postman.smtplib, "SMTP", refuse)
    with pytest.raises(OSError):
        send(desk, "localhost:1025")
    assert not list((desk / "outbox").glob("*.eml"))


def test_gate_names_the_vendor_on_a_hold_only_for_a_real_vendor_domain():
    from datetime import date
    vendors = [gate.Vendor("V-FULCRUM", "Fulcrum Software", "fulcrum.example", r"^FUL-\d{5}$", True)]
    rec = (Path(__file__).parent.parent / "fixtures" / "reader" / "m28-fulcrum-bank.json").read_text()
    d = gate.check(rec, "fulcrum.example", vendors, date(2026, 11, 7), 1)
    assert isinstance(d, gate.Held) and d.vendor_id == "V-FULCRUM" and "bank_change" in d.reasons
    d = gate.check(rec.replace('"fulcrum.example"', '"fulcrum-pay.example"'), "fulcrum-pay.example", vendors,
                   date(2026, 11, 7), 1)
    assert isinstance(d, gate.Held) and d.vendor_id is None
