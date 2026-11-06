"""The attack corpus: one .eml per case plus a manifest that labels it. Models only ever see the .eml."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIR = Path("eval/corpus")


@dataclass(frozen=True)
class Item:
    id: str
    kind: str  # attack | clean
    technique: str
    goal: str | None
    expect: str  # pay | hold_or_owed | no_harm
    claim: dict | None  # the invoice the email wants processed; what a fully hijacked reader would report
    history: tuple[dict, ...]  # invoices already paid before this email arrives
    text: str


def load(directory: Path = DEFAULT_DIR, only: list[str] | None = None) -> list[Item]:
    manifest = json.loads((directory / "manifest.json").read_text())
    items = [Item(m["id"], m["kind"], m["technique"], m["goal"], m["expect"], m["claim"], tuple(m["history"]),
                  (directory / f"{m['id']}.eml").read_text()) for m in manifest]
    if only:
        wanted = set(only)
        items = [i for i in items if i.id in wanted or i.id.split("-")[0] in wanted]
        if not items:
            raise SystemExit(f"no corpus item matches {', '.join(sorted(wanted))}")
    return items
