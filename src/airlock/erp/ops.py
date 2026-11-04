"""ERP operations. Pure functions over a connection; the CLI only prints their results."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from airlock.erp.db import audit, now, today
from airlock.schema import ClearedRecord, format_money


class ErpError(Exception):
    """A refusal the clerk should read as-is."""


@dataclass
class MatchResult:
    status: str  # matched | exception | duplicate
    invoice_id: str | None
    vendor: str
    invoice_no: str
    po: str
    reasons: list[str] = field(default_factory=list)
    billed_qty: int = 0
    received_qty: int = 0
    billed_cents: int = 0
    expected_cents: int = 0
    currency: str = "USD"

    def line(self) -> str:
        """The exact line the clerk posts. Wording is fixed here, not left to the model."""
        base = f"{self.invoice_id or '-'} {self.vendor} {self.invoice_no} {self.po}"
        if self.status == "matched":
            return f"MATCHED {base} pay={format_money(self.expected_cents)} {self.currency}"
        if self.status == "duplicate":
            return f"DUPLICATE {base} already on file; not re-entered"
        return (
            f"EXCEPTION {base} reasons={','.join(self.reasons)} billed_qty={self.billed_qty} "
            f"received_qty={self.received_qty} billed={format_money(self.billed_cents)} "
            f"expected={format_money(self.expected_cents)} {self.currency}"
        )


def _next_id(conn: sqlite3.Connection, table: str, prefix: str, width: int) -> str:
    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1
    return f"{prefix}{n:0{width}d}"


def match(conn: sqlite3.Connection, rec: ClearedRecord, actor: str = "ap-clerk") -> MatchResult:
    """Three-way match: invoice vs purchase order vs goods receipt. ERP values are the truth."""
    vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (rec.vendor_id,)).fetchone()
    if vendor is None:
        raise ErpError(f"unknown vendor {rec.vendor_id}")
    res = MatchResult("matched", None, vendor["name"], rec.vendor_invoice_no, rec.po_number,
                      billed_qty=rec.qty, billed_cents=rec.amount_cents, currency=rec.currency)

    dup = conn.execute("SELECT id FROM invoices WHERE vendor_id=? AND vendor_invoice_no=?",
                       (rec.vendor_id, rec.vendor_invoice_no)).fetchone()
    if dup:
        res.status, res.invoice_id = "duplicate", dup["id"]
        audit(conn, actor, "duplicate", dup["id"], rec.record_id)
        conn.commit()
        return res

    po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (rec.po_number,)).fetchone()
    reasons: list[str] = []
    pay = 0
    if po is None:
        reasons.append("po_unknown")
    elif po["vendor_id"] != rec.vendor_id:
        reasons.append("po_vendor_mismatch")
    else:
        got = conn.execute("SELECT COALESCE(SUM(qty_received),0) FROM goods_receipts WHERE po_id=?",
                           (po["id"],)).fetchone()[0]
        res.received_qty = got
        pay = got * po["unit_price_cents"]
        res.expected_cents = pay
        if rec.currency != po["currency"]:
            reasons.append("currency_mismatch")
        if got == 0:
            reasons.append("no_receipt")
        elif rec.qty != got:
            reasons.append("qty_mismatch")
        if rec.qty == got and rec.amount_cents != pay:
            reasons.append("price_mismatch")

    res.reasons = reasons
    res.status = "exception" if reasons else "matched"
    inv_id = _next_id(conn, "invoices", "I-", 4)
    conn.execute(
        "INSERT INTO invoices (id, vendor_id, vendor_invoice_no, po_id, qty, amount_cents, currency,"
        " due_date, status, reasons, pay_cents, record_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (inv_id, rec.vendor_id, rec.vendor_invoice_no, rec.po_number, rec.qty, rec.amount_cents,
         rec.currency, rec.due_date.isoformat(), res.status, ",".join(reasons), pay or None, rec.record_id),
    )
    res.invoice_id = inv_id
    audit(conn, actor, res.status, inv_id, ",".join(reasons))
    conn.commit()
    return res


def resolve(conn: sqlite3.Connection, invoice_id: str, action: str, by: str) -> str:
    """A person's decision on an exception. Only quantity/price exceptions can be short-paid."""
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if inv is None:
        raise ErpError(f"unknown invoice {invoice_id}")
    if inv["status"] != "exception":
        raise ErpError(f"{invoice_id} is {inv['status']}, not an exception")
    if action == "reject":
        conn.execute("UPDATE invoices SET status='rejected' WHERE id=?", (invoice_id,))
        audit(conn, by, "reject", invoice_id)
        conn.commit()
        return f"REJECTED {invoice_id} by={by}"
    if action != "short-pay":
        raise ErpError("action must be short-pay or reject")
    allowed = {"qty_mismatch", "price_mismatch"}
    if not set(inv["reasons"].split(",")) <= allowed or not inv["pay_cents"]:
        raise ErpError(f"{invoice_id} reasons={inv['reasons']} cannot be short-paid")
    short = inv["amount_cents"] - inv["pay_cents"]
    conn.execute("UPDATE invoices SET status='matched' WHERE id=?", (invoice_id,))
    conn.execute("INSERT INTO credit_notes (vendor_id, invoice_id, amount_cents) VALUES (?,?,?)",
                 (inv["vendor_id"], invoice_id, max(short, 0)))
    audit(conn, by, "short-pay", invoice_id, f"pay={format_money(inv['pay_cents'])} credit={format_money(max(short, 0))}")
    conn.commit()
    return (f"SHORT-PAID {invoice_id} pay={format_money(inv['pay_cents'])} {inv['currency']} "
            f"credit_note_requested={format_money(max(short, 0))} by={by}")


def propose_batch(conn: sqlite3.Connection, actor: str = "ap-clerk") -> tuple[str, str]:
    """Collect matched invoices into one batch. Returns (batch_id, document text)."""
    rows = conn.execute(
        "SELECT i.*, v.name AS vname, v.iban AS iban FROM invoices i JOIN vendors v ON v.id=i.vendor_id"
        " WHERE i.status='matched' ORDER BY i.id").fetchall()
    if not rows:
        raise ErpError("nothing matched to batch")
    day = today().isoformat()
    used = conn.execute("SELECT COUNT(*) FROM payment_batches WHERE id LIKE ?", (day + "-%",)).fetchone()[0]
    batch_id = f"{day}-{chr(ord('A') + used)}"
    n = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    lines, total = [], 0
    for r in rows:
        n += 1
        pid = f"PAY-{n:04d}"
        conn.execute("INSERT INTO payments (id, batch_id, invoice_id, vendor_id, payee_iban, amount_cents, currency)"
                     " VALUES (?,?,?,?,?,?,?)", (pid, batch_id, r["id"], r["vendor_id"], r["iban"], r["pay_cents"], r["currency"]))
        conn.execute("UPDATE invoices SET status='batched' WHERE id=?", (r["id"],))
        lines.append(f"{pid} | {r['vname']} | {r['vendor_invoice_no']} | {format_money(r['pay_cents'])} {r['currency']}")
        total += r["pay_cents"]
    conn.execute("INSERT INTO payment_batches VALUES (?,?,?, 'proposed')", (batch_id, now(), total))
    audit(conn, actor, "batch", batch_id, f"{len(rows)} payments total={format_money(total)}")
    conn.commit()
    doc = (f"# Payment batch {batch_id}\n\nPay each line with `bank pay <payment> --to <vendor> --amount <amount>`,"
           f" one command per payment.\n\n" + "\n".join(lines) + f"\n\ntotal | {format_money(total)} USD | {len(rows)} payments\n")
    return batch_id, doc


def reconcile(conn: sqlite3.Connection, receipts: Path, actor: str = "ap-clerk") -> list[str]:
    """Mark payments paid from the receipts treasury's bank tool dropped in our inbox."""
    if not receipts.exists():
        return []
    out = []
    for raw in receipts.read_text().splitlines():
        if not raw.strip():
            continue
        r = json.loads(raw)
        p = conn.execute("SELECT * FROM payments WHERE id=?", (r["payment_id"],)).fetchone()
        if p is None or p["bank_tx_id"]:
            continue
        conn.execute("UPDATE payments SET bank_tx_id=?, approved_by=?, executed_at=? WHERE id=?",
                     (r["tx_id"], r["approved_by"], r["executed_at"], p["id"]))
        conn.execute("UPDATE invoices SET status='paid' WHERE id=?", (p["invoice_id"],))
        audit(conn, actor, "paid", p["id"], f"tx={r['tx_id']} approved_by={r['approved_by']}")
        out.append(f"PAID {p['id']} {p['invoice_id']} tx={r['tx_id']} approved_by={r['approved_by']}")
    conn.execute("UPDATE payment_batches SET status='paid' WHERE id IN ("
                 "SELECT b.id FROM payment_batches b WHERE NOT EXISTS ("
                 "SELECT 1 FROM payments p WHERE p.batch_id=b.id AND p.bank_tx_id IS NULL))")
    conn.commit()
    return out


def decision_log(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
    lines = ["# AP decision log", ""]
    lines += [f"- {r['ts']} | {r['actor']} | {r['action']} | {r['ref']} | {r['detail']}" for r in rows]
    open_cn = conn.execute("SELECT c.*, v.name FROM credit_notes c JOIN vendors v ON v.id=c.vendor_id"
                           " WHERE c.status='open' AND c.amount_cents>0").fetchall()
    if open_cn:
        lines += ["", "## Open credit notes"]
        lines += [f"- {c['name']} | {c['invoice_id']} | {format_money(c['amount_cents'])}" for c in open_cn]
    return "\n".join(lines) + "\n"
