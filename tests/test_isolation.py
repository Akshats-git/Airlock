"""Can the clerk pay by running treasury's bank tool itself, skipping the approval card?

The clerk and treasury run as the same OS user on one machine, so only a sandbox stops it. This runs the
real `./bin/bank` from the clerk's folder twice: unsandboxed (it pays, so the test is meaningful) and
inside Codex's `workspace-write` sandbox, which is what Switch gives a Codex agent (nothing moves).
No model is called; `codex sandbox` only applies the sandbox. Skipped where Codex isn't installed.

The folders live under the repo, not under /tmp: the sandbox always lets an agent write to /tmp.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from airlock.bootstrap import install
from airlock.erp import ops as erp_ops
from airlock.erp.db import connect as erp_connect
from tests.conftest import ACME

PAY = ["../treasury/bin/bank", "pay", "PAY-0001", "--to", "Acme Corp", "--amount", "12400.00"]
codex = shutil.which("codex")


@pytest.fixture
def agents(monkeypatch):
    root = Path(__file__).resolve().parent.parent / "runtime" / f"isolation-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("AIRLOCK_TODAY", "2026-11-07")
    install(root)
    erp = erp_connect(root / "ap-clerk" / "erp.sqlite")
    erp_ops.match(erp, ACME)
    erp_ops.propose_batch(erp)
    erp.close()
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _transfers(root: Path) -> int:
    with sqlite3.connect(f"file:{root / 'treasury' / 'bank.sqlite'}?mode=ro", uri=True) as c:
        return c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]


def test_unsandboxed_clerk_could_pay_without_a_card(agents):
    """The hole a Claude Code clerk has on a shared machine: nothing but the model's own judgment."""
    r = subprocess.run(PAY, cwd=agents / "ap-clerk", capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.startswith("PAID PAY-0001"), r.stdout + r.stderr
    assert _transfers(agents) == 1


@pytest.mark.skipif(codex is None, reason="Codex is not installed")
def test_codex_sandboxed_clerk_cannot_pay(agents):
    r = subprocess.run([codex, "sandbox", "-c", 'sandbox_mode="workspace-write"', "--", *PAY],
                       cwd=agents / "ap-clerk", capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "PAID" not in r.stdout
    assert _transfers(agents) == 0
