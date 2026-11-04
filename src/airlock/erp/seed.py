"""Synthetic vendors, purchase orders and receipts. Deterministic; `make reset` re-runs it."""

from __future__ import annotations

import sqlite3

# (id, name, domain, phone, invoice pattern, iban)
VENDORS = [
    ("V-ACME", "Acme Corp", "acme.example", "+1-555-0101", r"^ACM-\d{5}$", "GB82WEST12345698765432"),
    ("V-GLOBEX", "Globex Ltd", "globex.example", "+44-20-5550-102", r"^GLX-\d{6}$", "DE89370400440532013000"),
    ("V-NORTHW", "Northwind Traders", "northwind.example", "+1-555-0103", r"^NW-\d{4}$", "FR1420041010050500013M02606"),
    ("V-INITECH", "Initech Inc", "initech.example", "+1-555-0104", r"^INV-\d{5}$", "NL91ABNA0417164300"),
]
# (po, vendor, sku, qty, unit price cents)
POS = [
    ("PO-10001", "V-ACME", "WIDGET-A", 100, 12400),
    ("PO-10002", "V-GLOBEX", "GADGET-G", 50, 8600),
    ("PO-10003", "V-NORTHW", "BOLT-N", 100, 5500),
    ("PO-10442", "V-INITECH", "LICENSE-I", 100, 48200),
]
# (receipt, po, qty received) -- Northwind billed 120, received 100
RECEIPTS = [("GR-1", "PO-10001", 100), ("GR-2", "PO-10002", 50), ("GR-3", "PO-10003", 100), ("GR-4", "PO-10442", 100)]


def seed(conn: sqlite3.Connection) -> None:
    for id_, name, dom, phone, pat, iban in VENDORS:
        conn.execute(
            "INSERT INTO vendors VALUES (?,?,?,?,?,?,1,'2026-01-15','active')",
            (id_, name, dom, phone, pat, iban),
        )
    for po, vendor, sku, qty, price in POS:
        conn.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,'USD','open')", (po, vendor, sku, qty, price))
    for gr, po, qty in RECEIPTS:
        conn.execute("INSERT INTO goods_receipts VALUES (?,?,?,'2026-11-03')", (gr, po, qty))
    conn.commit()
