"""The payment files a bank portal accepts: ISO 20022 pain.001 (credit transfer) and pain.002 (status).

Real AP teams don't call a bank per payment; they upload one credit-transfer file per run and read the bank's
status report back. Every bank command that moves money writes both, in treasury's folder:

    payment-files/<message id>.pain001.xml   what was instructed: debtor, each payee's verified account, amount,
                                             and the vendor's invoice number as the remittance text
    payment-files/<message id>.pain002.xml   the mock bank's answer: each transfer settled (ACSC), with its tx id

Both validate against the published ISO schemas (pain.001.001.09, pain.002.001.10; see tests/xsd).
The values come from the bank's own ledger rows, written after its checks passed, never from the batch document.
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

from airlock.schema import format_money

PAIN001 = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
PAIN002 = "urn:iso:std:iso:20022:tech:xsd:pain.002.001.10"

# The paying company and its own account (test values; the IBAN passes its checksum).
DEBTOR_NAME = "Airlock Demo Co"
DEBTOR_IBAN = "GB29NWBK60161331926819"
DEBTOR_BIC = "NWBKGB2L"


def _el(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    e = ET.SubElement(parent, tag, attrs)
    if text is not None:
        e.text = text
    return e


def _path(parent: ET.Element, tags: str, text: str) -> ET.Element:
    """_path(x, "Id/IBAN", v) builds <Id><IBAN>v</IBAN></Id> under x."""
    *outer, last = tags.split("/")
    for t in outer:
        parent = _el(parent, t)
    return _el(parent, last, text)


def _iso(ts: str) -> str:
    return ts.rstrip("Z")  # ISODateTime without zone: the bank's local reading of our UTC stamp is not at stake


def transfers(bank: sqlite3.Connection, erp: sqlite3.Connection, payment_ids: list[str]) -> list[dict]:
    """The ledger rows for these payments, each with the vendor's invoice number for the remittance text."""
    out = []
    for pid in payment_ids:
        t = bank.execute("SELECT * FROM ledger WHERE payment_id=?", (pid,)).fetchone()
        inv = erp.execute("SELECT i.vendor_invoice_no FROM payments p JOIN invoices i ON i.id=p.invoice_id"
                          " WHERE p.id=?", (pid,)).fetchone()
        out.append({**dict(t), "invoice_no": inv[0] if inv else pid})
    return out


def pain001(msg_id: str, created: str, execution_date: str, txs: list[dict]) -> bytes:
    total = format_money(sum(t["amount_cents"] for t in txs))
    doc = ET.Element("Document", xmlns=PAIN001)
    root = _el(doc, "CstmrCdtTrfInitn")
    hdr = _el(root, "GrpHdr")
    _el(hdr, "MsgId", msg_id)
    _el(hdr, "CreDtTm", _iso(created))
    _el(hdr, "NbOfTxs", str(len(txs)))
    _el(hdr, "CtrlSum", total)
    _path(hdr, "InitgPty/Nm", DEBTOR_NAME)

    inf = _el(root, "PmtInf")
    _el(inf, "PmtInfId", msg_id)
    _el(inf, "PmtMtd", "TRF")
    _el(inf, "BtchBookg", "false")  # one statement line per vendor, not one for the run
    _el(inf, "NbOfTxs", str(len(txs)))
    _el(inf, "CtrlSum", total)
    _path(inf, "ReqdExctnDt/Dt", execution_date)
    _path(inf, "Dbtr/Nm", DEBTOR_NAME)
    _path(inf, "DbtrAcct/Id/IBAN", DEBTOR_IBAN)
    _path(inf, "DbtrAgt/FinInstnId/BICFI", DEBTOR_BIC)
    _el(inf, "ChrgBr", "SLEV")
    for t in txs:
        tx = _el(inf, "CdtTrfTxInf")
        pid = _el(tx, "PmtId")
        _el(pid, "InstrId", t["payment_id"])
        _el(pid, "EndToEndId", t["payment_id"])
        _el(_el(tx, "Amt"), "InstdAmt", format_money(t["amount_cents"]), Ccy=t["currency"])
        _path(tx, "Cdtr/Nm", t["payee"])
        _path(tx, "CdtrAcct/Id/IBAN", t["iban"])
        _path(tx, "RmtInf/Ustrd", f"Invoice {t['invoice_no']}")
    return _bytes(doc)


def pain002(msg_id: str, created: str, txs: list[dict]) -> bytes:
    """The mock bank's status report: every transfer in the file settled."""
    total = format_money(sum(t["amount_cents"] for t in txs))
    doc = ET.Element("Document", xmlns=PAIN002)
    root = _el(doc, "CstmrPmtStsRpt")
    hdr = _el(root, "GrpHdr")
    _el(hdr, "MsgId", f"STS-{msg_id}"[:35])
    _el(hdr, "CreDtTm", _iso(created))
    _path(hdr, "DbtrAgt/FinInstnId/BICFI", DEBTOR_BIC)
    grp = _el(root, "OrgnlGrpInfAndSts")
    _el(grp, "OrgnlMsgId", msg_id)
    _el(grp, "OrgnlMsgNmId", "pain.001.001.09")
    _el(grp, "OrgnlNbOfTxs", str(len(txs)))
    _el(grp, "OrgnlCtrlSum", total)
    _el(grp, "GrpSts", "ACSC")
    inf = _el(root, "OrgnlPmtInfAndSts")
    _el(inf, "OrgnlPmtInfId", msg_id)
    _el(inf, "PmtInfSts", "ACSC")
    for t in txs:
        st = _el(inf, "TxInfAndSts")
        _el(st, "OrgnlInstrId", t["payment_id"])
        _el(st, "OrgnlEndToEndId", t["payment_id"])
        _el(st, "TxSts", "ACSC")
        _el(st, "AccptncDtTm", _iso(t["executed_at"]))
        _el(st, "AcctSvcrRef", t["tx_id"])
    return _bytes(doc)


def _bytes(doc: ET.Element) -> bytes:
    ET.indent(doc)
    return ET.tostring(doc, encoding="UTF-8", xml_declaration=True) + b"\n"


def write(bank: sqlite3.Connection, erp: sqlite3.Connection, msg_id: str, payment_ids: list[str],
          out_dir: Path, created: str, execution_date: str) -> str:
    """Write the run's pain.001 and pain.002. Returns the line the bank tool prints."""
    txs = transfers(bank, erp, payment_ids)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{msg_id}.pain001.xml").write_bytes(pain001(msg_id, created, execution_date, txs))
    (out_dir / f"{msg_id}.pain002.xml").write_bytes(pain002(msg_id, created, txs))
    return (f"FILE {out_dir.name}/{msg_id}.pain001.xml: ISO 20022 credit transfer, {len(txs)} payment"
            f"{'s' if len(txs) != 1 else ''}, {format_money(sum(t['amount_cents'] for t in txs))}; bank status ACSC")
