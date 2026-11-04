"""Mock ERP storage: SQLite in the clerk's own folder (Codex's sandbox has no network)."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email_domain TEXT NOT NULL UNIQUE,
  phone TEXT NOT NULL, invoice_no_pattern TEXT NOT NULL, iban TEXT NOT NULL,
  iban_verified INTEGER NOT NULL, iban_verified_at TEXT, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS purchase_orders (
  id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendors(id), sku TEXT NOT NULL,
  qty INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'USD',
  status TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS goods_receipts (
  id TEXT PRIMARY KEY, po_id TEXT NOT NULL REFERENCES purchase_orders(id),
  qty_received INTEGER NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL, vendor_invoice_no TEXT NOT NULL,
  po_id TEXT NOT NULL, qty INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL, due_date TEXT NOT NULL, status TEXT NOT NULL,
  reasons TEXT NOT NULL DEFAULT '', pay_cents INTEGER, record_id TEXT NOT NULL,
  UNIQUE (vendor_id, vendor_invoice_no));
CREATE TABLE IF NOT EXISTS payment_batches (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, total_cents INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, invoice_id TEXT NOT NULL, vendor_id TEXT NOT NULL,
  payee_iban TEXT NOT NULL, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL,
  bank_tx_id TEXT, approved_by TEXT, executed_at TEXT);
CREATE TABLE IF NOT EXISTS credit_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT NOT NULL, invoice_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, ref TEXT NOT NULL, detail TEXT NOT NULL);
"""


def erp_path() -> Path:
    return Path(os.environ.get("AIRLOCK_ERP", "erp.sqlite"))


def today() -> date:
    return date.fromisoformat(os.environ["AIRLOCK_TODAY"]) if "AIRLOCK_TODAY" in os.environ else date.today()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    p = path or erp_path()
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(p)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def audit(conn: sqlite3.Connection, actor: str, action: str, ref: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_events (ts, actor, action, ref, detail) VALUES (?,?,?,?,?)",
        (now(), actor, action, ref, detail),
    )
