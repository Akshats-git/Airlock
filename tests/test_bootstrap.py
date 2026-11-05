import os
import subprocess
import sys
from pathlib import Path

from airlock.agents.common import Names
from airlock.bootstrap import install


def test_wrappers_pin_each_agent_to_its_own_files(tmp_path):
    out = install(tmp_path, Names(), bin_dir=Path(sys.executable).parent)
    ap, bank = out["ap"].read_text(), out["bank"].read_text()
    assert f'AIRLOCK_ERP="{tmp_path}/ap-clerk/erp.sqlite"' in ap and "AIRLOCK_BANK" not in ap
    assert f'AIRLOCK_BANK="{tmp_path}/treasury/bank.sqlite"' in bank
    assert f'AIRLOCK_RECEIPTS="{tmp_path}/ap-clerk/inbox/receipts.jsonl"' in bank
    assert os.access(out["ap"], os.X_OK) and os.access(out["bank"], os.X_OK)


def test_the_wrappers_really_run_against_the_seeded_files(tmp_path):
    out = install(tmp_path)
    r = subprocess.run([str(out["bank"]), "balance"], capture_output=True, text=True, cwd=tmp_path / "treasury")
    assert r.returncode == 0 and r.stdout.startswith("BALANCE "), r.stderr
    r = subprocess.run([str(out["ap"]), "log"], capture_output=True, text=True, cwd=tmp_path / "ap-clerk")
    assert r.returncode == 0, r.stderr
