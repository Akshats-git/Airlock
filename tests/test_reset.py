from airlock.bank import ops as bank_ops
from airlock.erp.db import connect
from airlock.reset import reset_all


def test_reset_restores_opening_state(tmp_path):
    reset_all(tmp_path)
    bank = bank_ops.connect(tmp_path / "treasury" / "bank.sqlite")
    bank.execute("UPDATE meta SET v=1 WHERE k='balance'")
    bank.commit()
    bank.close()
    (tmp_path / "clerk" / "inbox").mkdir()
    (tmp_path / "clerk" / "inbox" / "receipts.jsonl").write_text("{}\n")

    reset_all(tmp_path)

    assert bank_ops.balance(bank_ops.connect(tmp_path / "treasury" / "bank.sqlite")) == bank_ops.OPENING_CENTS
    assert not (tmp_path / "clerk" / "inbox").exists()
    erp = connect(tmp_path / "clerk" / "erp.sqlite")
    assert erp.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == len(__import__("airlock.erp.seed", fromlist=["VENDORS"]).VENDORS)
    assert erp.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0
