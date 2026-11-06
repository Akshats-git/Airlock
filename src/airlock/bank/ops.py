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
  verified_at TEXT NOT NULL, verified_by TEXT NOT NULL);
"""
# Accounts confirmed when each vendor was onboarded, by a call to the number in the vendor master.
OPENING_VERIFIED = ("2026-01-15", "vendor onboarding call")


class BankError(Exception):
    """A refusal. The message is posted to the room as-is."""


def bank_path() -> Path:
    return Path(os.environ.get("AIRLOCK_BANK", "bank.sqlite"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or bank_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('balance', ?)", (OPENING_CENTS,))
    if conn.execute("SELECT 1 FROM beneficiaries LIMIT 1").fetchone() is None:
        conn.executemany("INSERT INTO beneficiaries VALUES (?,?,?,?,?)",
                         [(v[0], v[1], v[5], *OPENING_VERIFIED) for v in VENDORS])
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
        approver: str, receipts: Path | None = None) -> str:
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
    return f"PAID {payment_id} {b['name']} {format_money(cents)} {p['currency']} tx={tx} approved_by={approver}"


def snapshot(bank: sqlite3.Connection) -> dict:
    rows = bank.execute("SELECT * FROM ledger ORDER BY tx_id DESC").fetchall()
    return {"opening_cents": OPENING_CENTS, "balance_cents": balance(bank),
            "transfers": [dict(r) for r in rows]}
