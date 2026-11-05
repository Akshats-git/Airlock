"""`make reset`: restore the ERP, the bank and the receipts inbox to the opening state."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from airlock.bank import ops as bank_ops
from airlock.erp.db import connect
from airlock.erp.seed import seed


def _fresh(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for p in (path, Path(str(path) + "-journal")):
        p.unlink(missing_ok=True)


def reset_all(root: Path, clerk: str = "clerk", treasury: str = "treasury") -> None:
    erp = root / clerk / "erp.sqlite"
    _fresh(erp)
    conn = connect(erp)
    seed(conn)
    conn.close()

    bank = root / treasury / "bank.sqlite"
    _fresh(bank)
    bank_ops.connect(bank).close()

    inbox = root / clerk / "inbox"
    if inbox.exists():
        shutil.rmtree(inbox)
    for stale in (root / clerk).glob("batch-*.md"):
        stale.unlink()


def main() -> None:
    root = Path(os.environ.get("AIRLOCK_RUNTIME", "runtime"))
    reset_all(root)
    print(f"reset: ERP, bank and inbox under {root}/")


if __name__ == "__main__":
    main()
