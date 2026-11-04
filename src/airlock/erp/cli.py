"""`ap`: the clerk's only tool. Every command prints the exact line to post."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from airlock.erp import ops
from airlock.erp.db import connect
from airlock.schema import ClearedRecord

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Airlock mock ERP for the AP clerk.")


def _fail(msg: str) -> None:
    typer.echo(f"ERROR {msg}")
    raise typer.Exit(1)


@app.command("match")
def match_cmd(record: Annotated[str, typer.Argument(help="One cleared record as JSON, copied from the customs card.")]):
    """Three-way match one cleared record and enter it."""
    try:
        rec = ClearedRecord.model_validate_json(record)
    except ValidationError:
        _fail("record is not a valid cleared record; copy the JSON from the customs card exactly")
    try:
        typer.echo(ops.match(connect(), rec).line())
    except ops.ErpError as e:
        _fail(str(e))


@app.command("resolve")
def resolve_cmd(invoice: str, action: Annotated[str, typer.Argument(help="short-pay | reject")],
                by: Annotated[str, typer.Option("--by", help="Who decided (chat username).")]):
    """Record a person's decision on an exception."""
    try:
        typer.echo(ops.resolve(connect(), invoice, action, by))
    except ops.ErpError as e:
        _fail(str(e))


batch_app = typer.Typer(help="Payment batches.")
app.add_typer(batch_app, name="batch")


@batch_app.command("propose")
def batch_propose():
    """Batch every matched invoice; prints the document to save in the room."""
    try:
        batch_id, doc = ops.propose_batch(connect())
    except ops.ErpError as e:
        _fail(str(e))
    Path(f"batch-{batch_id}.md").write_text(doc)
    typer.echo(f"BATCH {batch_id} written to batch-{batch_id}.md\n")
    typer.echo(doc)


@app.command("reconcile")
def reconcile_cmd():
    """Mark payments paid from the receipts the bank tool left in ./inbox/receipts.jsonl."""
    path = Path(os.environ.get("AIRLOCK_RECEIPTS", "inbox/receipts.jsonl"))
    lines = ops.reconcile(connect(), path)
    typer.echo("\n".join(lines) if lines else "NOTHING NEW to reconcile")


@app.command("log")
def log_cmd():
    """Print the decision log as markdown (save it as the room's decision-log document)."""
    typer.echo(ops.decision_log(connect()))
