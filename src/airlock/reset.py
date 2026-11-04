"""`make reset`: restore the ERP (and later the bank and fixtures) to the opening state."""

from __future__ import annotations

import os
from pathlib import Path

from airlock.erp.db import connect
from airlock.erp.seed import seed


def reset_erp(path: Path) -> None:
    for p in (path, Path(str(path) + "-journal")):
        p.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    seed(conn)
    conn.close()


def main() -> None:
    root = Path(os.environ.get("AIRLOCK_RUNTIME", "runtime"))
    reset_erp(root / "clerk" / "erp.sqlite")
    print(f"reset: {root}/clerk/erp.sqlite")


if __name__ == "__main__":
    main()
