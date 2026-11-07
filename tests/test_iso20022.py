"""The payment files validate against the published ISO 20022 schemas and say exactly what the bank paid."""

from pathlib import Path

import pytest
from lxml import etree
from typer.testing import CliRunner

from airlock.bank import iso20022, ops
from airlock.bank.cli import app
from airlock.erp import ops as erp_ops
from tests.conftest import ACME, GLOBEX, NORTHW

XSD = Path(__file__).parent / "xsd"
NS1 = {"p": iso20022.PAIN001}
NS2 = {"p": iso20022.PAIN002}


def schema(name):
    return etree.XMLSchema(etree.parse(str(XSD / f"{name}.xsd")))


@pytest.fixture
def paid(erp, tmp_path, monkeypatch):
    """Acme, Globex and a short-paid Northwind batched; the routine run paid, then Acme on its own card."""
    monkeypatch.setenv("AIRLOCK_BANK", str(tmp_path / "bank.sqlite"))
    monkeypatch.setenv("AIRLOCK_RECEIPTS", str(tmp_path / "inbox" / "receipts.jsonl"))
    for rec in (ACME, GLOBEX, NORTHW):
        erp_ops.match(erp, rec)
    erp_ops.resolve(erp, "I-0003", "short-pay", "sam")
    doc = erp_ops.propose_batch(erp)[1]
    run = next(ln for ln in doc.splitlines() if ln.startswith("RUN ")).split()[2:]
    assert CliRunner().invoke(app, run).exit_code == 0
    r = CliRunner().invoke(app, ["pay", "PAY-0001", "--to", "Acme Corp", "--iban", "GB82WEST12345698765432",
                                 "--amount", "12400.00"])
    assert r.exit_code == 0, r.output
    return tmp_path / "payment-files"


def test_every_file_validates_against_the_iso_schema(paid):
    files = sorted(paid.glob("*.xml"))
    assert [f.name for f in files] == ["AIRLOCK-2026-11-07-A-RUN.pain001.xml", "AIRLOCK-2026-11-07-A-RUN.pain002.xml",
                                       "AIRLOCK-PAY-0001.pain001.xml", "AIRLOCK-PAY-0001.pain002.xml"]
    for f in files:
        kind = "pain.001.001.09" if ".pain001." in f.name else "pain.002.001.10"
        s = schema(kind)
        assert s.validate(etree.parse(str(f))), (f.name, s.error_log)


def test_the_run_file_carries_the_bank_view_of_each_payment(paid):
    x = etree.parse(str(paid / "AIRLOCK-2026-11-07-A-RUN.pain001.xml"))
    assert x.findtext(".//p:GrpHdr/p:NbOfTxs", namespaces=NS1) == "2"
    assert x.findtext(".//p:GrpHdr/p:CtrlSum", namespaces=NS1) == "9800.00"
    assert x.findtext(".//p:DbtrAcct/p:Id/p:IBAN", namespaces=NS1) == iso20022.DEBTOR_IBAN
    txs = [(t.findtext("p:PmtId/p:EndToEndId", namespaces=NS1), t.findtext("p:Cdtr/p:Nm", namespaces=NS1),
            t.findtext("p:CdtrAcct/p:Id/p:IBAN", namespaces=NS1), t.findtext("p:Amt/p:InstdAmt", namespaces=NS1),
            t.findtext("p:RmtInf/p:Ustrd", namespaces=NS1))
           for t in x.iterfind(".//p:CdtTrfTxInf", namespaces=NS1)]
    assert txs == [("PAY-0002", "Globex Ltd", "DE89370400440532013000", "4300.00", "Invoice GLX-200045"),
                   ("PAY-0003", "Northwind Traders", "FR1420041010050500013M02606", "5500.00", "Invoice NW-4410")]


def test_the_status_report_names_each_bank_transaction(paid):
    x = etree.parse(str(paid / "AIRLOCK-2026-11-07-A-RUN.pain002.xml"))
    assert x.findtext(".//p:OrgnlMsgId", namespaces=NS2) == "AIRLOCK-2026-11-07-A-RUN"
    sts = [(t.findtext("p:OrgnlEndToEndId", namespaces=NS2), t.findtext("p:TxSts", namespaces=NS2),
            t.findtext("p:AcctSvcrRef", namespaces=NS2)) for t in x.iterfind(".//p:TxInfAndSts", namespaces=NS2)]
    assert sts == [("PAY-0002", "ACSC", "TX-0001"), ("PAY-0003", "ACSC", "TX-0002")]


def test_a_refused_payment_writes_no_file(erp, tmp_path, monkeypatch):
    monkeypatch.setenv("AIRLOCK_BANK", str(tmp_path / "bank.sqlite"))
    erp_ops.match(erp, ACME)
    erp_ops.propose_batch(erp)
    r = CliRunner().invoke(app, ["pay", "PAY-0001", "--to", "Acme Corp", "--iban", "GB33BUKB20201555555555",
                                 "--amount", "12400.00"])
    assert r.exit_code == 1 and not (tmp_path / "payment-files").exists()
    assert ops.balance(ops.connect()) == ops.OPENING_CENTS


def test_the_debtor_account_passes_its_checksum():
    from airlock.schema import iban_ok
    assert iban_ok(iso20022.DEBTOR_IBAN)


def test_the_ledger_page_lists_and_serves_the_files_and_nothing_else(paid):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from airlock.bank import web

    srv = ThreadingHTTPServer(("127.0.0.1", 0), web._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        import json
        d = json.load(urllib.request.urlopen(base + "/api/ledger"))
        assert {f["pain001"] for f in d["files"]} == {"AIRLOCK-PAY-0001.pain001.xml", "AIRLOCK-2026-11-07-A-RUN.pain001.xml"}
        assert b"<CstmrPmtStsRpt>" in urllib.request.urlopen(base + "/files/AIRLOCK-PAY-0001.pain002.xml").read()
        for bad in ("/files/../bank.sqlite", "/files/AIRLOCK-PAY-0001.pain001.xml/../../bank.sqlite", "/files/x.xml"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(base + bad)
    finally:
        srv.shutdown()
