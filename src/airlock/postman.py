"""The postman: the letters a vendor gets from the desk, written by plain code outside every agent.

No agent sends mail. An agent that could write email could be talked into writing the wrong one, so the
postman builds each letter itself, from fixed wording and the records of whoever owns the fact:

    remittance advice          one per transfer in the bank's ledger (treasury's records)
    credit note request        one per short-paid invoice (the ERP's credit notes)
    bank-detail request seen   customs held mail from a vendor's own domain asking for new bank details
    bank details changed       a change the bank applied after the callback and the controller's approval

Every address is the one on the bank's beneficiary list, as the vendor was onboarded; no email can change it.
Letters go to <outbox>/<key>.eml, once each (the file is the record that it was sent), and, if AIRLOCK_SMTP is
set (host:port, for example a local Mailpit on localhost:1025), to that server as well.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import sqlite3
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from airlock.bank.iso20022 import DEBTOR_NAME
from airlock.schema import format_money

log = logging.getLogger("airlock.postman")

FROM = f"Accounts payable, {DEBTOR_NAME} <ap@airlock-demo.example>"
SIGN = f"Accounts payable\n{DEBTOR_NAME}"
NO_EMAIL_CHANGES = ("We never change bank details because of an email. If anything here looks wrong, call us on the "
                    "number you have for us.")


KINDS = ("bank-change-request", "bank-change-applied", "credit-note", "remittance")


@dataclass(frozen=True)
class Letter:
    key: str
    to: str
    subject: str
    body: str

    @property
    def kind(self) -> str:
        return next(k for k in KINDS if self.key.startswith(k + "-"))

    def message(self) -> EmailMessage:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = FROM, self.to, self.subject
        m["Date"] = formatdate(localtime=False)
        m["Message-ID"] = f"<{self.key}@airlock-demo.example>"
        m.set_content(self.body, cte="8bit")
        return m


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _ending(iban: str) -> str:
    return f"the account ending {iban[-4:]}"


def _contacts(bank: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["vendor_id"]: r for r in bank.execute("SELECT * FROM beneficiaries")}


def remittances(bank: sqlite3.Connection, erp: sqlite3.Connection | None) -> list[Letter]:
    out, who = [], _contacts(bank)
    for t in bank.execute("SELECT * FROM ledger ORDER BY tx_id"):
        inv = erp.execute("SELECT p.vendor_id, i.vendor_invoice_no, i.po_id FROM payments p JOIN invoices i"
                          " ON i.id=p.invoice_id WHERE p.id=?", (t["payment_id"],)).fetchone() if erp else None
        vendor = next((v for v in who.values() if v["name"] == t["payee"]), None)
        if inv is None or vendor is None:
            continue
        out.append(Letter(
            f"remittance-{t['tx_id']}", vendor["email"], f"Remittance advice: invoice {inv['vendor_invoice_no']}",
            f"Dear {t['payee']} accounts team,\n\nWe have paid your invoice {inv['vendor_invoice_no']} "
            f"(our order {inv['po_id']}).\n\n"
            f"  Amount           {format_money(t['amount_cents'])} {t['currency']}\n"
            f"  Paid to          {_ending(t['iban'])} (the account we hold on file for you)\n"
            f"  Date             {t['executed_at'][:10]}\n"
            f"  Our reference    {t['payment_id']}, bank transaction {t['tx_id']}\n\n"
            f"{NO_EMAIL_CHANGES}\n\n{SIGN}\n"))
    return out


def credit_notes(bank: sqlite3.Connection, erp: sqlite3.Connection | None) -> list[Letter]:
    if erp is None:
        return []
    who, out = _contacts(bank), []
    for c in erp.execute("SELECT c.amount_cents AS credit, i.* FROM credit_notes c JOIN invoices i ON i.id=c.invoice_id"
                         " WHERE c.amount_cents>0 ORDER BY c.id"):
        v = who.get(c["vendor_id"])
        if v is None:
            continue
        out.append(Letter(
            f"credit-note-{c['id']}", v["email"], f"Credit note request: invoice {c['vendor_invoice_no']}",
            f"Dear {v['name']} accounts team,\n\nYour invoice {c['vendor_invoice_no']} billed "
            f"{format_money(c['amount_cents'])} {c['currency']} for {c['qty']} units against our order {c['po_id']}. "
            f"We received fewer, so we are paying {format_money(c['pay_cents'])} {c['currency']}, the value of what "
            f"arrived.\n\nPlease send a credit note for {format_money(c['credit'])} {c['currency']}, quoting "
            f"{c['vendor_invoice_no']}.\n\n{SIGN}\n"))
    return out


def read_holds(path: Path) -> list[dict]:
    out = []
    for raw in path.read_text().splitlines() if path.exists() else []:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def bank_change_requests(bank: sqlite3.Connection, holds: list[dict]) -> list[Letter]:
    who, out, seen = _contacts(bank), [], set()
    for h in holds:
        v = who.get(h.get("vendor_id") or "")
        if v is None or "bank_change" not in h.get("reasons", []) or h["record_id"] in seen:
            continue
        seen.add(h["record_id"])
        out.append(Letter(
            f"bank-change-request-{h['record_id']}", v["email"], "We received a request to change your bank details",
            f"Dear {v['name']} accounts team,\n\nWe received an email from your domain asking us to change the bank "
            f"account we pay you. We have not changed anything: payments still go to {_ending(v['iban'])}.\n\n"
            f"Before any change, a member of our AP team will call you on the number we hold for you, {v['phone']}. "
            f"If you did not send this request, someone may be using your email address; please check your mailbox.\n\n"
            f"Our reference: {h['record_id']}.\n\n{SIGN}\n"))
    return out


def bank_changes_applied(bank: sqlite3.Connection) -> list[Letter]:
    who, out = _contacts(bank), []
    for c in bank.execute("SELECT * FROM beneficiary_changes WHERE status='applied' ORDER BY id"):
        v = who.get(c["vendor_id"])
        if v is None:
            continue
        out.append(Letter(
            f"bank-change-applied-{c['id']}", v["email"], "Your bank details with us were changed",
            f"Dear {v['name']} accounts team,\n\nOn {c['applied_at'][:10]} we changed the account we pay you from "
            f"{_ending(c['old_iban'])} to {_ending(c['new_iban'])}, after we called you on {c['called']} and a second "
            f"person approved the change.\n\nIf you did not ask for this, call us at once.\n\n"
            f"Our reference: {c['id']}.\n\n{SIGN}\n"))
    return out


def due(bank: sqlite3.Connection, erp: sqlite3.Connection | None, holds: list[dict]) -> list[Letter]:
    """Every letter the records call for, in the order a vendor would expect them."""
    return bank_change_requests(bank, holds) + credit_notes(bank, erp) + remittances(bank, erp) + bank_changes_applied(bank)


def letters(bank_path: Path, erp_path: Path, holds: Path) -> list[Letter]:
    bank = _ro(bank_path)
    if bank is None:
        return []
    erp = _ro(erp_path)
    try:
        return due(bank, erp, read_holds(holds))
    finally:
        bank.close()
        if erp:
            erp.close()


def deliver(new: list[Letter], outbox: Path, smtp: str | None = None) -> list[str]:
    """Send every letter not yet in the outbox. Returns one line per letter sent."""
    outbox.mkdir(parents=True, exist_ok=True)
    out = []
    for letter in new:
        path = outbox / f"{letter.key}.eml"
        if path.exists():
            continue
        msg = letter.message()
        if smtp:
            host, _, port = smtp.rpartition(":")
            with smtplib.SMTP(host or "localhost", int(port), timeout=10) as s:
                s.send_message(msg)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(bytes(msg))
        tmp.rename(path)
        out.append(f"SENT {letter.key} to {letter.to}: {letter.subject}")
    return out


def run_once(bank_path: Path, erp_path: Path, holds: Path, outbox: Path, smtp: str | None = None) -> list[str]:
    return deliver(letters(bank_path, erp_path, holds), outbox, smtp)


def watch(bank_path: Path, erp_path: Path, holds: Path, outbox: Path, every: float = 2.0) -> None:
    smtp = os.environ.get("AIRLOCK_SMTP") or None
    log.info("postman watching: letters to %s%s", outbox, f" and SMTP {smtp}" if smtp else "")
    while True:
        try:
            for line in run_once(bank_path, erp_path, holds, outbox, smtp):
                log.info(line)
        except (sqlite3.Error, OSError) as e:  # a locked database or a stopped mail server: try again shortly
            log.warning("postman: %s", e)
        time.sleep(every)
