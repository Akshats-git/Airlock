import json

import pytest

from airlock.erp.db import connect
from airlock.erp.seed import seed
from airlock.schema import ClearedRecord


@pytest.fixture
def erp(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRLOCK_ERP", str(tmp_path / "erp.sqlite"))
    monkeypatch.setenv("AIRLOCK_TODAY", "2026-11-07")
    conn = connect()
    seed(conn)
    yield conn
    conn.close()


def cleared(**over) -> ClearedRecord:
    base = dict(record_id="Q-20261107-001", vendor_id="V-ACME", vendor_invoice_no="ACM-10021",
                po_number="PO-10001", currency="USD", amount_cents=1240000, qty=100, due_date="2026-11-21")
    return ClearedRecord.model_validate_json(json.dumps({**base, **over}))


ACME = cleared()
GLOBEX = cleared(record_id="Q-20261107-002", vendor_id="V-GLOBEX", vendor_invoice_no="GLX-200045",
                 po_number="PO-10002", amount_cents=430000, qty=50)
NORTHW = cleared(record_id="Q-20261107-003", vendor_id="V-NORTHW", vendor_invoice_no="NW-4410",
                 po_number="PO-10003", amount_cents=660000, qty=120)
