"""Mock bank (test mode). It trusts the ERP's verified payee and amount, never the caller's."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from airlock.erp.db import now
from airlock.schema import format_money

OPENING_CENTS = 100_000_000  # $1,000,000.00

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
  tx_id TEXT PRIMARY KEY, payment_id TEXT NOT NULL UNIQUE, payee TEXT NOT NULL, iban TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, approved_by TEXT NOT NULL, executed_at TEXT NOT NULL);
"""


class BankError(Exception):
    """A refusal. The message is posted to the room as-is."""


def bank_path() -> Path:
    return Path(os.environ.get("AIRLOCK_BANK", "bank.sqlite"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or bank_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('balance', ?)", (OPENING_CENTS,))
    conn.commit()
    return conn


def balance(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT v FROM meta WHERE k='balance'").fetchone()[0]


def parse_amount(text: str) -> int:
    if not re.fullmatch(r"\d{1,9}\.\d{2}", text) or int(text.replace(".", "")) <= 0:
        raise BankError(f"amount {text!r} must be positive with exactly two decimals, like 12400.00")
    return int(text.replace(".", ""))


def pay(bank: sqlite3.Connection, erp: sqlite3.Connection, payment_id: str, to: str, amount: str,
        approver: str, receipts: Path | None = None) -> str:
    cap = int(os.environ.get("AIRLOCK_PAY_CAP_CENTS", "5000000"))
    cents = parse_amount(amount)

    p = erp.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if p is None:
        raise BankError(f"{payment_id} is not in any batch")
    done = bank.execute("SELECT * FROM ledger WHERE payment_id=?", (payment_id,)).fetchone()
    if done:
        return f"ALREADY PAID {payment_id} tx={done['tx_id']}; nothing moved"
    v = erp.execute("SELECT * FROM vendors WHERE id=?", (p["vendor_id"],)).fetchone()
    if to != v["name"]:
        raise BankError(f"{payment_id} payee is {v['name']!r}, not {to!r}")
    if cents != p["amount_cents"]:
        raise BankError(f"{payment_id} amount is {format_money(p['amount_cents'])}, not {amount}")
    if not v["iban_verified"] or v["status"] != "active":
        raise BankError(f"{v['name']} has no verified bank account on file")
    if p["payee_iban"] != v["iban"]:
        raise BankError(f"{payment_id} account differs from the verified account")
    if cents > cap:
        raise BankError(f"{amount} is over the per-payment cap of {format_money(cap)}")
    if cents > balance(bank):
        raise BankError("insufficient funds")

    n = bank.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] + 1
    tx, ts = f"TX-{n:04d}", now()
    bank.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?)",
                 (tx, payment_id, v["name"], v["iban"], cents, p["currency"], approver, ts))
    bank.execute("UPDATE meta SET v=v-? WHERE k='balance'", (cents,))
    bank.commit()
    if receipts:
        receipts.parent.mkdir(parents=True, exist_ok=True)
        with receipts.open("a") as f:
            f.write(json.dumps(dict(payment_id=payment_id, tx_id=tx, approved_by=approver, executed_at=ts)) + "\n")
    return f"PAID {payment_id} {v['name']} {format_money(cents)} {p['currency']} tx={tx} approved_by={approver}"


def snapshot(bank: sqlite3.Connection) -> dict:
    rows = bank.execute("SELECT * FROM ledger ORDER BY tx_id DESC").fetchall()
    return {"opening_cents": OPENING_CENTS, "balance_cents": balance(bank),
            "transfers": [dict(r) for r in rows]}
