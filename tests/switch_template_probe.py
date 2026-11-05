"""Runs under Switch's own Python (it has switch_core and yaml). Prints lint findings and the compiled walls as JSON."""
import json
import sys

import yaml
from switch_core import template_v2
from switch_core.template_lint import lint_template

text = open(sys.argv[1]).read()
lint = lint_template(text)
builtins = {"$creator": "ctrl", "$creator_email": "c@x", "$date": "2026-11-07", "$timestamp": "0", "$agents_dir": "/AG"}
compiled = template_v2.compile_v2(text, {}, builtins)
doc = yaml.safe_load(compiled.document)
print(json.dumps({
    "errors": [f.code for f in lint.errors],
    "warnings": [f.code for f in lint.warnings],
    "agents": {a["name"]: {k: a.get(k) for k in ("provider", "addressing", "auto_mode", "auto_approve")} for a in doc["agents"]},
    "rooms": {r["name"]: {"agents": r["agents"], "private": r.get("channel_type"), "docs": [d["name"] for d in r.get("docs", [])]}
              for r in doc["rooms"]},
    "room_addressing": compiled.room_addressing,
}))
