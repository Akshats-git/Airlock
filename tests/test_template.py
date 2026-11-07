"""The template's walls, checked with Switch's own parser (skipped when Switch is not installed).

These are the settings the whole design rests on: who may instruct treasury, who is in which room,
and that nothing is approved without a person. Renaming an agent in one place and not the other fails here.
"""
import json
import subprocess
from pathlib import Path

import pytest

from airlock.agents.common import Names

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template" / "airlock.template.yaml"
CORES = sorted(Path.home().glob(".config/switchdash/local-switch-server/native/switch-core/*"))


@pytest.fixture(scope="module")
def compiled():
    if not CORES:
        pytest.skip("Switch is not installed on this machine")
    core = CORES[-1]
    out = subprocess.run([str(core / "venv/bin/python"), str(ROOT / "tests/switch_template_probe.py"), str(TEMPLATE)],
                         capture_output=True, text=True, env={"PYTHONPATH": str(core / "src/core")}, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_switch_finds_nothing_to_complain_about(compiled):
    assert compiled["errors"] == [] and compiled["warnings"] == []


def test_only_the_controller_can_instruct_treasury_and_every_payment_asks(compiled):
    t = compiled["agents"][Names().treasury]
    assert t == {"provider": "claude", "addressing": "owner", "auto_mode": False, "auto_approve": False}


def test_no_agent_runs_in_bypass_mode(compiled):
    assert all(a["auto_approve"] is False for a in compiled["agents"].values())


def test_the_clerk_is_addressable_only_from_the_desk(compiled):
    assert compiled["room_addressing"] == {Names().clerk: [Names().desk]}


def test_room_membership_is_the_wall(compiled):
    n, rooms = Names(), compiled["rooms"]
    q, d = set(rooms[n.quarantine]["agents"]), set(rooms[n.desk]["agents"])
    assert q == {n.mailroom, n.reader, n.customs}
    assert d == {n.clerk, n.treasury, n.customs, n.red_team}
    assert q & d == {n.customs}  # customs is the only member of both rooms
    assert all(r["private"] == "channel_private" for r in rooms.values())


def test_the_policy_is_a_template_document_so_no_agent_can_edit_it(compiled):
    assert compiled["rooms"][Names().desk]["docs"] == ["ap-policy"]


def test_the_clerk_can_only_run_in_codexs_sandbox(compiled):
    # Claude Code has no sandbox: a clerk on it could run treasury's bank tool without a card.
    assert compiled["clerk"] == {"provider": "codex", "model": "gpt-6-luna"}
    assert "clerk_provider" not in TEMPLATE.read_text()
