"""Synthetic vendors, purchase orders and receipts. Deterministic; `make reset` re-runs it."""

from __future__ import annotations

import sqlite3

# (id, name, domain, phone, invoice pattern, iban)
VENDORS = [
    ("V-ACME", "Acme Corp", "acme.example", "+1-555-0101", r"^ACM-\d{5}$", "GB82WEST12345698765432"),
    ("V-GLOBEX", "Globex Ltd", "globex.example", "+44-20-5550-102", r"^GLX-\d{6}$", "DE89370400440532013000"),
    ("V-NORTHW", "Northwind Traders", "northwind.example", "+1-555-0103", r"^NW-\d{4}$", "FR1420041010050500013M02606"),
    ("V-INITECH", "Initech Inc", "initech.example", "+1-555-0104", r"^INV-\d{5}$", "NL91ABNA0417164300"),
    ("V-BLUEFIN", "Bluefin Logistics", "bluefin.example", "+1-555-0105", r"^BFL-\d{5}$", "GB94LOYD30964712345678"),
    ("V-COBALT", "Cobalt Office Supply", "cobalt.example", "+1-555-0106", r"^COB-\d{4}$", "GB64HBUK40127698765432"),
    ("V-DELTA", "Delta Facilities", "deltafm.example", "+49-30-5550-107", r"^DFM-\d{6}$", "DE91500105175432198765"),
    ("V-EVERGRN", "Evergreen Catering", "evergreen.example", "+353-1-555-0108", r"^EVG-\d{4}$", "IE29AIBK93115212345678"),
    ("V-FULCRUM", "Fulcrum Software", "fulcrum.example", "+44-20-5550-109", r"^FUL-\d{5}$", "GB82NWBK60161354781296"),
    ("V-GRANITE", "Granite Freight", "granite.example", "+31-20-555-0110", r"^GRF-\d{5}$", "NL95RABO0310724556"),
]
# A normal morning's open orders (fixtures/morning): mostly routine, a few large, one short delivery, one dispute.
MORNING_POS = [
    ("PO-20001", "V-BLUEFIN", "FREIGHT-RUN", 3, 95000), ("PO-20002", "V-BLUEFIN", "FREIGHT-RUN", 4, 103000),
    ("PO-20003", "V-BLUEFIN", "CHARTER", 2, 730000),
    ("PO-20004", "V-COBALT", "PAPER-A4", 40, 1531), ("PO-20005", "V-COBALT", "TONER", 120, 875),
    ("PO-20006", "V-COBALT", "CHAIR", 25, 12900), ("PO-20023", "V-COBALT", "WHITEBOARD", 10, 4500),
    ("PO-20007", "V-DELTA", "CLEANING-MONTH", 1, 640000), ("PO-20008", "V-DELTA", "SECURITY-SHIFT", 12, 31000),
    ("PO-20009", "V-DELTA", "HVAC-REPAIR", 1, 1890000), ("PO-20026", "V-DELTA", "PLUMBING", 1, 220000),
    ("PO-20010", "V-EVERGRN", "LUNCH", 60, 2450), ("PO-20011", "V-EVERGRN", "LUNCH", 45, 2450),
    ("PO-20012", "V-EVERGRN", "EVENT-COVER", 200, 1800), ("PO-20025", "V-EVERGRN", "LUNCH", 100, 2450),
    ("PO-20013", "V-FULCRUM", "SEAT-MONTH", 50, 7200), ("PO-20014", "V-FULCRUM", "SUPPORT-PLAN", 1, 985000),
    ("PO-20015", "V-FULCRUM", "ANNUAL-LICENSE", 1, 2750000),
    ("PO-20016", "V-GRANITE", "PALLET-ROUTE", 6, 64000), ("PO-20017", "V-GRANITE", "PALLET-ROUTE", 4, 64000),
    ("PO-20018", "V-GRANITE", "PALLET-ROUTE", 9, 64000), ("PO-20024", "V-GRANITE", "PALLET-ROUTE", 2, 64000),
    ("PO-20019", "V-ACME", "WIDGET-A", 30, 12400), ("PO-20020", "V-GLOBEX", "GADGET-G", 20, 8600),
    ("PO-20021", "V-NORTHW", "BOLT-N", 60, 5500), ("PO-20022", "V-INITECH", "LICENSE-I", 5, 48200),
]
MORNING_RECEIVED = {"PO-20025": 80}  # Evergreen delivered 80 of 100 lunches

# (po, vendor, sku, qty, unit price cents)
POS = [
    ("PO-10001", "V-ACME", "WIDGET-A", 100, 12400),
    ("PO-10002", "V-GLOBEX", "GADGET-G", 50, 8600),
    ("PO-10003", "V-NORTHW", "BOLT-N", 100, 5500),
    ("PO-10442", "V-INITECH", "LICENSE-I", 100, 48200),
] + MORNING_POS
# (receipt, po, qty received) -- Northwind billed 120, received 100
RECEIPTS = [("GR-1", "PO-10001", 100), ("GR-2", "PO-10002", 50), ("GR-3", "PO-10003", 100), ("GR-4", "PO-10442", 100)] + [
    (f"GR-{po[3:]}", po, MORNING_RECEIVED.get(po, qty)) for po, _v, _s, qty, _p in MORNING_POS]


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
