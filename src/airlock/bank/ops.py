"""Mock bank (test mode).

It pays only to accounts on its own beneficiary list, which lives in treasury's folder, out of the clerk's
reach. The ERP (the clerk's) says what is owed and to which vendor; it does not get to say where money goes.
A hijacked clerk that rewrites its own ERP can at worst ask the bank to pay a real vendor, to that vendor's
verified account, an amount the controller sees on the approval card.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from airlock.erp.db import now
from airlock.erp.seed import VENDORS
from airlock.schema import format_money

OPENING_CENTS = 100_000_000  # $1,000,000.00

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
  tx_id TEXT PRIMARY KEY, payment_id TEXT NOT NULL UNIQUE, payee TEXT NOT NULL, iban TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, approved_by TEXT NOT NULL, executed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS beneficiaries (
  vendor_id TEXT PRIMARY KEY, name TEXT NOT NULL, iban TEXT NOT NULL,
  verified_at TEXT NOT NULL, verified_by TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS beneficiary_changes (
  id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL, old_iban TEXT NOT NULL, new_iban TEXT NOT NULL,
  called TEXT NOT NULL, verified_by TEXT NOT NULL, verified_at TEXT NOT NULL,
  status TEXT NOT NULL, applied_at TEXT, approved_by TEXT);
"""
# Accounts confirmed when each vendor was onboarded, by a call to the number in the vendor master.
OPENING_VERIFIED = ("2026-01-15", "vendor onboarding call")


class BankError(Exception):
    """A refusal. The message is posted to the room as-is."""


def bank_path() -> Path:
    return Path(os.environ.get("AIRLOCK_BANK", "bank.sqlite"))


def files_dir() -> Path:
    """Where the ISO 20022 payment files go: next to the bank, in treasury's folder."""
    return Path(os.environ.get("AIRLOCK_PAYMENT_FILES") or bank_path().parent / "payment-files")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or bank_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('balance', ?)", (OPENING_CENTS,))
    if conn.execute("SELECT 1 FROM beneficiaries LIMIT 1").fetchone() is None:
        conn.executemany("INSERT INTO beneficiaries (vendor_id, name, iban, verified_at, verified_by, phone, email)"
                         " VALUES (?,?,?,?,?,?,?)",
                         [(v[0], v[1], v[5], *OPENING_VERIFIED, v[3], f"accounts@{v[2]}") for v in VENDORS])
    conn.commit()
    return conn


def balance(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT v FROM meta WHERE k='balance'").fetchone()[0]


def parse_amount(text: str) -> int:
    if not re.fullmatch(r"\d{1,9}\.\d{2}", text) or int(text.replace(".", "")) <= 0:
        raise BankError(f"amount {text!r} must be positive with exactly two decimals, like 12400.00")
    return int(text.replace(".", ""))


def beneficiary(bank: sqlite3.Connection, vendor_id: str) -> sqlite3.Row | None:
    return bank.execute("SELECT * FROM beneficiaries WHERE vendor_id=?", (vendor_id,)).fetchone()


def pay(bank: sqlite3.Connection, erp: sqlite3.Connection, payment_id: str, to: str, iban: str, amount: str,
        approver: str, receipts: Path | None = None, files: Path | None = None) -> str:
    cap = int(os.environ.get("AIRLOCK_PAY_CAP_CENTS", "5000000"))
    cents = parse_amount(amount)

    p = erp.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if p is None:
        raise BankError(f"{payment_id} is not in any batch")
    done = bank.execute("SELECT * FROM ledger WHERE payment_id=?", (payment_id,)).fetchone()
    if done:
        return f"ALREADY PAID {payment_id} tx={done['tx_id']}; nothing moved"
    b = beneficiary(bank, p["vendor_id"])
    if b is None:
        raise BankError(f"{payment_id}: vendor {p['vendor_id']} has no verified account on the bank's beneficiary list")
    if to != b["name"]:
        raise BankError(f"{payment_id} payee is {b['name']!r}, not {to!r}")
    if iban != b["iban"]:
        raise BankError(f"{iban} is not {b['name']}'s verified account; the bank pays only its beneficiary list")
    if p["payee_iban"] != b["iban"]:
        raise BankError(f"{payment_id}: the batch names account {p['payee_iban']}, not {b['name']}'s verified account")
    if cents != p["amount_cents"]:
        raise BankError(f"{payment_id} amount is {format_money(p['amount_cents'])}, not {amount}")
    if cents > cap:
        raise BankError(f"{amount} is over the per-payment cap of {format_money(cap)}")
    if cents > balance(bank):
        raise BankError("insufficient funds")

    n = bank.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] + 1
    tx, ts = f"TX-{n:04d}", now()
    bank.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?)",
                 (tx, payment_id, b["name"], b["iban"], cents, p["currency"], approver, ts))
    bank.execute("UPDATE meta SET v=v-? WHERE k='balance'", (cents,))
    bank.commit()
    if receipts:
        receipts.parent.mkdir(parents=True, exist_ok=True)
        with receipts.open("a") as f:
            f.write(json.dumps(dict(payment_id=payment_id, tx_id=tx, approved_by=approver, executed_at=ts)) + "\n")
    out = f"PAID {payment_id} {b['name']} {format_money(cents)} {p['currency']} tx={tx} approved_by={approver}"
    if files:
        from airlock.bank import iso20022
        from airlock.erp.db import today
        out += "\n" + iso20022.write(bank, erp, f"AIRLOCK-{payment_id}", [payment_id], files, ts, today().isoformat())
    return out


def pay_run(bank: sqlite3.Connection, erp: sqlite3.Connection, batch_id: str, count: int, total: str, digest: str,
            approver: str, receipts: Path | None = None, files: Path | None = None) -> str:
    """Pay a batch's routine payments on one approval.

    The bank decides which payments are routine from its own records (amount, and when the account it holds was
    last verified), not from the batch document. The card's count, total and fingerprint must match that set
    exactly, so the controller approved precisely these payments; then each is paid with every check `pay` makes.
    """
    from airlock import risk
    from airlock.erp.db import today

    rows = erp.execute("SELECT * FROM payments WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
    if not rows:
        raise BankError(f"batch {batch_id} has no payments")
    run = []
    for p in rows:
        b = beneficiary(bank, p["vendor_id"])
        if b is None or p["payee_iban"] != b["iban"]:
            continue  # not routine: `pay` will refuse it on its own card
        if risk.own_card_reason(p["amount_cents"], b["verified_at"], today()) is None:
            run.append((p, b))
    lines = [(p["id"], p["vendor_id"], b["iban"], p["amount_cents"]) for p, b in run]
    want = (len(run), format_money(sum(p["amount_cents"] for p, _ in run)), risk.digest(lines))
    if (count, total, digest) != want:
        raise BankError(f"this run doesn't match the bank's own view of batch {batch_id}: {want[0]} routine payments, "
                        f"total {want[1]}, digest {want[2]}; nothing was paid")
    out = [pay(bank, erp, p["id"], b["name"], b["iban"], format_money(p["amount_cents"]), approver, receipts)
           for p, b in run]
    out.append(f"RUN {batch_id}: {len(run)} routine payments, {total} USD, approved_by={approver}")
    paid = [p["id"] for (p, _), line in zip(run, out) if line.startswith("PAID")]
    if files and paid:
        from airlock.bank import iso20022
        out.append(iso20022.write(bank, erp, f"AIRLOCK-{batch_id}-RUN", paid, files, now(), today().isoformat()))
    return "\n".join(out)


# -- changing a beneficiary's account: two people, neither of them a model --------------------------------
#
# 1. The AP specialist phones the vendor on the number the BANK holds for it (not one from an email) and posts
#    the result. Customs, plain code, records it here only if Switch says the AP specialist posted it.
# 2. The controller has treasury apply it with `bank change apply`, which shows an approval card only the
#    controller can answer. The bank refuses a change nobody verified, and one verified by the controller.


def record_verification(bank: sqlite3.Connection, vendor_id: str, new_iban: str, called: str,
                        verified_by: str) -> str:
    """The specialist's callback, as customs relays it. Returns the line customs posts."""
    from airlock.schema import iban_ok

    b = beneficiary(bank, vendor_id)
    if b is None:
        raise BankError(f"{vendor_id} is not on the bank's beneficiary list")
    if not iban_ok(new_iban):
        raise BankError(f"{new_iban} fails the IBAN checksum")
    if new_iban == b["iban"]:
        raise BankError(f"{new_iban} is already {b['name']}'s account")
    if called != b["phone"]:
        raise BankError(f"{called} is not the number on file for {b['name']}; call {b['phone']}")
    bank.execute("UPDATE beneficiary_changes SET status='superseded' WHERE vendor_id=? AND status='pending'",
                 (vendor_id,))
    n = bank.execute("SELECT COUNT(*) FROM beneficiary_changes").fetchone()[0] + 1
    cid = f"BC-{n:04d}"
    bank.execute("INSERT INTO beneficiary_changes VALUES (?,?,?,?,?,?,?, 'pending', NULL, NULL)",
                 (cid, vendor_id, b["iban"], new_iban, called, verified_by, now()))
    bank.commit()
    return (f"BANK DETAILS VERIFIED {cid} {b['name']} {b['iban']} -> {new_iban} called {called} (the number on "
            f"file) by {verified_by}. Not applied: the controller has treasury apply {cid}, on an approval card.")


def apply_change(bank: sqlite3.Connection, erp: sqlite3.Connection | None, change_id: str, to: str, iban: str,
                 approver: str, controller: str | None = None) -> str:
    """Treasury's half, behind the controller's approval card. ``erp`` gets the new account as its copy."""
    c = bank.execute("SELECT * FROM beneficiary_changes WHERE id=?", (change_id,)).fetchone()
    if c is None:
        raise BankError(f"{change_id} does not exist; a change needs the AP specialist's callback first")
    if c["status"] != "pending":
        raise BankError(f"{change_id} is {c['status']}, not pending")
    b = beneficiary(bank, c["vendor_id"])
    if to != b["name"]:
        raise BankError(f"{change_id} is for {b['name']!r}, not {to!r}")
    if iban != c["new_iban"]:
        raise BankError(f"{change_id} verified {c['new_iban']}, not {iban}")
    if controller and c["verified_by"].casefold() == controller.casefold():
        raise BankError(f"{change_id} was verified by {c['verified_by']}, the controller; a second person must call")
    ts = now()
    bank.execute("UPDATE beneficiaries SET iban=?, verified_at=?, verified_by=? WHERE vendor_id=?",
                 (iban, ts[:10], f"{c['verified_by']}, callback {c['called']}; {change_id}", c["vendor_id"]))
    bank.execute("UPDATE beneficiary_changes SET status='applied', applied_at=?, approved_by=? WHERE id=?",
                 (ts, approver, change_id))
    bank.commit()
    if erp is not None:  # the ERP's account is a copy the bank checks; keep it in step so batches carry it
        erp.execute("UPDATE vendors SET iban=?, iban_verified=1, iban_verified_at=? WHERE id=?",
                    (iban, ts[:10], c["vendor_id"]))
        erp.execute("INSERT INTO audit_events (ts, actor, action, ref, detail) VALUES (?,?,?,?,?)",
                    (ts, "bank", "beneficiary_change", change_id, f"{c['old_iban']} -> {iban} verified_by="
                     f"{c['verified_by']} approved_by={approver}"))
        erp.commit()
    return (f"APPLIED {change_id} {b['name']} now pays to {iban} (was {c['old_iban']}); verified_by={c['verified_by']} "
            f"approved_by={approver}")


def snapshot(bank: sqlite3.Connection) -> dict:
    rows = bank.execute("SELECT * FROM ledger ORDER BY tx_id DESC").fetchall()
    payees = bank.execute("SELECT vendor_id, name, iban, verified_at, verified_by FROM beneficiaries ORDER BY name")
    changes = bank.execute("SELECT * FROM beneficiary_changes WHERE status='pending' ORDER BY id")
    return {"opening_cents": OPENING_CENTS, "balance_cents": balance(bank),
            "transfers": [dict(r) for r in rows], "beneficiaries": [dict(r) for r in payees],
            "pending_changes": [dict(r) for r in changes]}
