"""`bank`: treasury's only tool. Every command asks the controller first: one routine payment run per batch, and
one command, with its account, for every payment that needs its own look."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from airlock.bank import ops
from airlock.erp.db import connect as erp_connect, erp_path
from airlock.schema import format_money

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Airlock mock bank for the treasury agent.")


def _approver() -> str:
    """Who a payment is recorded as approved by. A card can only be answered by someone Switch lets address
    treasury, and treasury is owner-only, so the approver is its owner, the controller (named at bootstrap).
    `airlock doctor` and `verify` check that treasury is still owner-only and the name still matches Switch."""
    return os.environ.get("AIRLOCK_APPROVER") or os.environ.get("AIRLOCK_CONTROLLER") or "switch-approval"


@app.command("pay")
def pay_cmd(payment_id: str,
            to: Annotated[str, typer.Option("--to", help="Payee name exactly as in the batch document.")],
            iban: Annotated[str, typer.Option("--iban", help="The account, as in the batch document. Shown on the approval card.")],
            amount: Annotated[str, typer.Option("--amount", help="Amount as in the batch document, e.g. 12400.00")]):
    """Pay one approved item, once, and only to an account on the bank's own beneficiary list."""
    receipts = os.environ.get("AIRLOCK_RECEIPTS")
    approver = _approver()
    try:
        erp = erp_connect(erp_path(), readonly=True)
        typer.echo(ops.pay(ops.connect(), erp, payment_id, to, iban, amount, approver, Path(receipts) if receipts else None,
                         ops.files_dir()))
    except ops.BankError as e:
        typer.echo(f"REFUSED {e}")
        raise typer.Exit(1)


@app.command("pay-run")
def pay_run_cmd(batch_id: str,
                count: Annotated[int, typer.Option("--count", help="How many routine payments, as in the batch document.")],
                total: Annotated[str, typer.Option("--total", help="Their total, as in the batch document.")],
                digest: Annotated[str, typer.Option("--digest", help="The run's fingerprint, as in the batch document.")]):
    """Pay a batch's routine payments on one approval, if they are exactly what the card says."""
    receipts = os.environ.get("AIRLOCK_RECEIPTS")
    approver = _approver()
    try:
        erp = erp_connect(erp_path(), readonly=True)
        typer.echo(ops.pay_run(ops.connect(), erp, batch_id, count, total, digest, approver,
                               Path(receipts) if receipts else None, ops.files_dir()))
    except ops.BankError as e:
        typer.echo(f"REFUSED {e}")
        raise typer.Exit(1)


change_app = typer.Typer(no_args_is_help=True, help="Change a beneficiary's account (after the specialist's callback).")
app.add_typer(change_app, name="change")


@change_app.command("apply")
def change_apply_cmd(change_id: str,
                     to: Annotated[str, typer.Option("--to", help="The vendor's name, as in the BANK DETAILS VERIFIED line.")],
                     iban: Annotated[str, typer.Option("--iban", help="The new account, as in that line. Shown on the approval card.")]):
    """Apply one verified change to the beneficiary list."""
    approver = _approver()
    try:
        erp = erp_connect(erp_path()) if erp_path().exists() else None
        typer.echo(ops.apply_change(ops.connect(), erp, change_id, to, iban, approver,
                                    os.environ.get("AIRLOCK_CONTROLLER")))
    except ops.BankError as e:
        typer.echo(f"REFUSED {e}")
        raise typer.Exit(1)


@app.command("balance")
def balance_cmd():
    """Print the account balance."""
    typer.echo(f"BALANCE {format_money(ops.balance(ops.connect()))} USD")


@app.command("serve")
def serve_cmd(port: int = 8099):
    """Serve the live ledger page."""
    from airlock.bank.web import serve

    serve(port)
