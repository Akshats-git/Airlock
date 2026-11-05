"""`airlock`: register and run the four protocol agents.

    airlock register                      # once; needs SWITCH_REG_KEY (a Gateway registration key)
    airlock customs                       # leave running
    airlock reader --backend fixture      # leave running; --backend openai for the real model
    airlock redteam                       # leave running; mention it in the desk to fire an attack
    airlock mailroom send                 # post the fixture emails into quarantine
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from airlock.agents import customs as customs_agent
from airlock.agents import mailroom as mailroom_agent
from airlock.agents import reader as reader_agent
from airlock.agents import redteam as redteam_agent
from airlock.agents.common import AGENT_DESCRIPTIONS, Names, creds_path, load_agent, register_agent, resolve_room
from airlock.protocol.client import HttpError

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Airlock protocol agents.")
mailroom_app = typer.Typer(help="The mailroom.", no_args_is_help=True)
app.add_typer(mailroom_app, name="mailroom")


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")


@app.command()
def register():
    """Register mailroom, reader, customs and red-team with your own Gateway registration key."""
    key = os.environ.get("SWITCH_REG_KEY")
    names = Names.from_env()
    for name in (names.mailroom, names.reader, names.customs, names.red_team):
        if creds_path(name).exists():
            typer.echo(f"{name}: already registered ({creds_path(name)})")
            continue
        if not key:
            typer.echo("SWITCH_REG_KEY is not set: create a registration key in the Gateway and export it")
            raise typer.Exit(1)
        try:
            register_agent(name, key)
        except HttpError as e:
            typer.echo(f"{name}: {e}")
            raise typer.Exit(1)
        typer.echo(f"{name}: registered")
    assert set(AGENT_DESCRIPTIONS) == {names.mailroom, names.reader, names.customs, names.red_team}


@app.command()
def bootstrap(agents_dir: Annotated[Path, typer.Option(help="Where Console keeps agent folders.")] = Path.home() / ".switch/agents/localhost-43381"):
    """After running the template: reset the ERP and bank into the agents' folders and install ./bin/ap and ./bin/bank."""
    from airlock.bootstrap import install

    for name, path in install(agents_dir).items():
        typer.echo(f"{name}: {path}")


@app.command()
def customs():
    """Run the schema gate."""
    asyncio.run(customs_agent.run())


@app.command()
def reader(backend: Annotated[str, typer.Option(help="fixture (free, replays recorded records) or openai")] = "fixture",
           fixtures: Annotated[Path, typer.Option(help="Recorded records, for the fixture backend.")] = Path("fixtures/reader")):
    """Run the tool-less reader."""
    asyncio.run(reader_agent.run(backend, fixtures))


@app.command("reader-models")
def reader_models():
    """List the OpenAI model ids this key can use (free), to pick AIRLOCK_READER_MODEL."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        typer.echo("OPENAI_API_KEY is not set")
        raise typer.Exit(1)
    typer.echo("\n".join(reader_agent.list_models(key)))


@app.command()
def redteam():
    """Run the scripted attacker (mention it in the desk to fire)."""
    asyncio.run(redteam_agent.run())


@app.command("redteam-fire")
def redteam_fire(attack: Annotated[int, typer.Argument(help="1-based attack number")] = 1):
    """Fire one attack into the desk right now, without the red team listening."""
    names = Names.from_env()
    agent = load_agent(names.red_team)
    desk = resolve_room(agent, names.desk)
    typer.echo(redteam_agent.fire(agent, desk, names, attack - 1))


@mailroom_app.command("send")
def mailroom_send(directory: Annotated[Path, typer.Option(help="Folder of .eml files.")] = Path("fixtures/emails"),
                  only: Annotated[list[str] | None, typer.Option(help="Mail ids to send, e.g. 01-acme.")] = None,
                  delay: Annotated[float, typer.Option(help="Seconds between posts.")] = 0.5):
    """Post the emails into quarantine as one run."""
    run = mailroom_agent.run_send(directory, only, delay)
    typer.echo(f"posted run {run}")


if __name__ == "__main__":
    app()
