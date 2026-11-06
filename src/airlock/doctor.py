"""`airlock doctor`: check, against the live server, that the desk is wired the way the README claims.

Every check reads from Switch itself (rooms, members, addressing) or from disk, never from our own config,
so a pass means the walls are really there on this machine.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from airlock.agents.common import Names

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass(frozen=True)
class Check:
    status: str
    label: str
    hint: str = ""

    def line(self) -> str:
        return f"{_MARK[self.status]} {self.label}" + (f"\n    -> {self.hint}" if self.hint else "")


def registrations(exists: Callable[[str], bool], names: Names) -> list[Check]:
    missing = [n for n in (names.mailroom, names.reader, names.customs, names.red_team) if not exists(n)]
    if missing:
        return [Check(FAIL, f"protocol agents not registered: {', '.join(missing)}",
                      "uv run airlock reg-key && uv run airlock register")]
    return [Check(OK, "four protocol agents registered")]


def _one_room(rooms: list[dict], name: str) -> tuple[dict | None, Check | None]:
    live = [r for r in rooms if r.get("name") == name and not r.get("archived")]
    if not live:
        return None, Check(FAIL, f"no room {name}", "run the Airlock template in Switch Console")
    if len(live) > 1:
        return None, Check(FAIL, f"{len(live)} rooms named {name}", "archive the old ones")
    return live[0], None


def walls(rooms: list[dict], detail: Callable[[str], dict], names: Names) -> list[Check]:
    """Who is in which room. Membership is the wall, so this is the check that matters most."""
    out: list[Check] = []
    q, bad = _one_room(rooms, names.quarantine)
    out += [bad] if bad else []
    d, bad = _one_room(rooms, names.desk)
    out += [bad] if bad else []
    if q is None or d is None:
        return out
    qd, dd = detail(q["room_id"]), detail(d["room_id"])
    q_agents, d_agents = set(qd.get("agent_names", [])), set(dd.get("agent_names", []))

    breach = q_agents & {names.clerk, names.treasury, names.red_team}
    if breach:
        out.append(Check(FAIL, f"wall breached: {', '.join(sorted(breach))} can read {names.quarantine}",
                         "remove them from the quarantine room"))
    want_q = {names.mailroom, names.reader, names.customs}
    if want_q - q_agents:
        out.append(Check(FAIL, f"{names.quarantine} is missing {', '.join(sorted(want_q - q_agents))}",
                         f"!invite-agent them in {names.quarantine}"))
    elif not breach:
        out.append(Check(OK, f"{names.quarantine}: only mailroom, reader and customs read vendor mail"))

    leak = d_agents & {names.reader, names.mailroom}
    if leak:
        out.append(Check(FAIL, f"wall breached: {', '.join(sorted(leak))} is in {names.desk}",
                         "remove it from the desk"))
    want_d = {names.clerk, names.treasury, names.customs, names.red_team}
    if want_d - d_agents:
        out.append(Check(FAIL, f"{names.desk} is missing {', '.join(sorted(want_d - d_agents))}",
                         "re-run the template, or !invite-agent them"))
    elif not leak:
        out.append(Check(OK, f"{names.desk}: clerk, treasury, customs, red-team; customs is the only agent in both"))

    for det, name in ((qd, names.quarantine), (dd, names.desk)):
        if det.get("channel_type") != "channel_private":
            out.append(Check(WARN, f"{name} is not a private channel", "anyone in the workspace can read it"))

    statuses = {**qd.get("agent_statuses", {}), **dd.get("agent_statuses", {})}
    needed = [names.customs, names.reader, names.red_team, names.clerk, names.treasury]
    down = [n for n in needed if n in statuses and statuses[n] != "live"]
    if down:
        out.append(Check(WARN, f"not live: {', '.join(f'{n} ({statuses[n]})' for n in down)}",
                         "scripts/take.sh reset starts customs, reader and red-team; Console runs clerk and treasury"))
    else:
        out.append(Check(OK, "every agent the run needs is live"))
    return out


def _owner_only(policy: dict | None) -> bool:
    rules = (policy or {}).get("rules") or []
    return bool(rules) and all(r.get("owner") and not r.get("owner_agents") and not r.get("users")
                               and not r.get("agents") and not r.get("platform") for r in rules)


def addressing(agents: list[dict], names: Names) -> list[Check]:
    by_name = {a.get("name"): a for a in agents}
    out: list[Check] = []
    t = by_name.get(names.treasury)
    if t is None:
        out.append(Check(FAIL, "treasury does not exist", "run the Airlock template in Switch Console"))
    elif _owner_only(t.get("addressing_policy")):
        out.append(Check(OK, "treasury takes instructions from its owner only (not even the owner's agents)"))
    else:
        out.append(Check(FAIL, "treasury accepts instructions from someone other than its owner",
                         "set treasury's 'Who can talk to your agent' to 'Only me' in Console"))
    c = by_name.get(names.clerk)
    if c is not None:
        kind = str(c.get("connector_type", ""))
        if "codex" in kind.casefold():
            out.append(Check(OK, "the clerk runs on Codex, in a sandbox that can't write treasury's files"))
        else:
            out.append(Check(WARN, f"the clerk runs on {kind or 'an unknown provider'}, with no sandbox",
                             "fine for a demo; for real money run it on Codex (see README, Limits)"))
    return out


def files(agents: Path, names: Names) -> list[Check]:
    clerk, treasury = agents / names.clerk, agents / names.treasury
    need = [clerk / "bin" / "ap", clerk / "erp.sqlite", treasury / "bin" / "bank", treasury / "bank.sqlite"]
    missing = [str(p.relative_to(agents)) for p in need if not p.exists()]
    if missing:
        return [Check(FAIL, f"missing in {agents}: {', '.join(missing)}", "scripts/take.sh reset")]
    out = [Check(OK, f"ERP, bank and the ./bin/ap and ./bin/bank tools are in {agents}")]
    try:
        with sqlite3.connect(f"file:{treasury / 'bank.sqlite'}?mode=ro", uri=True) as conn:
            cents = conn.execute("SELECT v FROM meta WHERE k='balance'").fetchone()[0]
            paid = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
            payees = conn.execute("SELECT COUNT(*) FROM beneficiaries").fetchone()[0]
        status = OK if paid == 0 else WARN
        out.append(Check(status, f"bank balance {cents // 100:,}.{cents % 100:02d}, {paid} transfers",
                         "" if paid == 0 else "scripts/take.sh reset for a clean take"))
        out.append(Check(OK if payees else FAIL, f"bank beneficiary list: {payees} verified accounts, in treasury's folder",
                         "" if payees else "scripts/take.sh reset"))
    except sqlite3.Error as e:
        out.append(Check(FAIL, f"bank unreadable: {e}", "scripts/take.sh reset"))
    return out


def listeners(pid_dir: Path) -> list[Check]:
    down = []
    for name in ("customs", "reader", "redteam", "ledger"):
        try:
            os.kill(int((pid_dir / f"{name}.pid").read_text()), 0)
        except (OSError, ValueError):
            down.append(name)
    if down:
        return [Check(WARN, f"not running here: {', '.join(down)}", "scripts/take.sh reset")]
    return [Check(OK, "customs, reader, red-team and the ledger page are running")]


def report(checks: list[Check]) -> tuple[str, bool]:
    text = "\n".join(c.line() for c in checks)
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    text += f"\n\n{len(checks) - failed - warned} ok, {warned} warnings, {failed} failures"
    return text, failed == 0
