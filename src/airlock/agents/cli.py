"""`airlock`: register and run the four protocol agents.

    airlock reg-key                       # once; signs in as the server admin, saves a registration key
    airlock register                      # once; registers the four protocol agents with that key
    airlock doctor                        # checks the walls on the live server
    airlock archive-rooms                 # before a fresh take: archive the old ap-quarantine and ap-desk
    airlock customs                       # leave running
    airlock reader --backend fixture      # leave running; --backend openai for the real model
    airlock redteam                       # leave running; mention it in the desk to fire an attack
    airlock mailroom send                 # post the fixture emails into quarantine
    airlock eval airlock --reader quiet   # the attack corpus through Airlock's code (offline, free)
    airlock eval naive --prompt guarded   # the same corpus against one tool-using agent (OpenAI, cents)
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
from airlock import gateway, local
from airlock.agents.common import (AGENT_DESCRIPTIONS, Names, api_url, creds_path, load_agent, register_agent,
                                   resolve_room, runtime_dir)
from airlock.protocol.client import HttpError

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Airlock protocol agents.")
mailroom_app = typer.Typer(help="The mailroom.", no_args_is_help=True)
app.add_typer(mailroom_app, name="mailroom")
eval_app = typer.Typer(help="Fire the attack corpus at Airlock and at a single-agent baseline.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")


@app.command()
def register():
    """Register mailroom, reader, customs and red-team with your own Gateway registration key."""
    key = os.environ.get("SWITCH_REG_KEY") or gateway.load_key(gateway.key_file(runtime_dir()))
    names = Names.from_env()
    for name in (names.mailroom, names.reader, names.customs, names.red_team):
        if creds_path(name).exists():
            typer.echo(f"{name}: already registered ({creds_path(name)})")
            continue
        if not key:
            typer.echo("No registration key: run `uv run airlock reg-key` (or export SWITCH_REG_KEY)")
            raise typer.Exit(1)
        try:
            register_agent(name, key)
        except HttpError as e:
            typer.echo(f"{name}: {e}")
            raise typer.Exit(1)
        typer.echo(f"{name}: registered")
    assert set(AGENT_DESCRIPTIONS) == {names.mailroom, names.reader, names.customs, names.red_team}


def _found(fn):
    try:
        return fn()
    except local.NotFound as e:
        typer.echo(str(e))
        raise typer.Exit(1)


@app.command("reg-key")
def reg_key(email: Annotated[str | None, typer.Option(help="The server admin's email (or SWITCH_ADMIN_EMAIL).")] = None):
    """Sign in to your Switch server as its admin and save a registration key in runtime/. Never printed."""
    email = email or os.environ.get("SWITCH_ADMIN_EMAIL") or typer.prompt("Switch admin email")
    password = os.environ.get("SWITCH_ADMIN_PASSWORD") or typer.prompt("Password", hide_input=True)
    api = _found(api_url)
    try:
        key, created = gateway.registration_key(api, gateway.login(api, email, password))
    except gateway.GatewayError as e:
        typer.echo(f"Couldn't get a key: {e}")
        raise typer.Exit(1)
    path = gateway.key_file(runtime_dir())
    gateway.save_key(path, key)
    typer.echo(f"{'Created' if created else 'Found'} the '{gateway.LABEL}' registration key on {api}; saved to {path} "
               f"(only you can read it). Next: uv run airlock register")


@app.command()
def paths(shell: Annotated[bool, typer.Option("--shell", help="Print as shell exports.")] = False):
    """Where the server and the agents' folders are on this machine."""
    api = _found(api_url)
    names = Names.from_env()
    agents = _found(lambda: local.agents_dir(api=api, clerk=names.clerk, treasury=names.treasury))
    values = {"AIRLOCK_API": api, "AIRLOCK_AGENTS_DIR": agents,
              **local.data_paths(agents, names.clerk, names.treasury)}
    for k, v in values.items():
        typer.echo(f'export {k}="{v}"' if shell else f"{k}={v}")


@app.command()
def bootstrap(agents_dir: Annotated[Path | None, typer.Option(help="Where Console keeps agent folders (found automatically).")] = None):
    """After running the template: reset the ERP and bank into the agents' folders and install ./bin/ap and ./bin/bank."""
    from airlock.bootstrap import install

    names = Names.from_env()
    if agents_dir is None:
        api = _found(api_url)
        agents_dir = _found(lambda: local.agents_dir(api=api, clerk=names.clerk, treasury=names.treasury))
    for name, path in install(agents_dir, names).items():
        typer.echo(f"{name}: {path}")


@app.command()
def doctor():
    """Check against the live server that the walls are where the README says, and that a take can start."""
    from airlock import doctor as dr

    names = Names.from_env()
    checks: list[dr.Check] = []
    try:
        api = local.api_url()
        checks.append(dr.Check(dr.OK, f"Switch server at {api}"))
    except local.NotFound as e:
        typer.echo(dr.Check(dr.FAIL, "Switch server not found", str(e)).line())
        raise typer.Exit(1)
    checks += dr.registrations(lambda n: creds_path(n).exists(), names)
    if checks[-1].status == dr.OK:
        try:
            agent = load_agent(names.customs)
            checks += dr.walls(agent.call("list_rooms"), lambda rid: agent.call("get_room_detail", room_id=rid), names)
            checks += dr.addressing(agent.call("list_agents"), names)
        except (HttpError, OSError) as e:
            checks.append(dr.Check(dr.FAIL, f"the server refused customs: {e}", "is the local server running?"))
    try:
        agents = local.agents_dir(api=api, clerk=names.clerk, treasury=names.treasury)
        checks += dr.files(agents, names)
    except local.NotFound as e:
        checks.append(dr.Check(dr.FAIL, "agents' folders not found", str(e)))
    checks += dr.listeners(runtime_dir() / "pids")
    text, ok = dr.report(checks)
    typer.echo(text)
    raise typer.Exit(0 if ok else 1)


@app.command("archive-rooms")
def archive_rooms(yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first.")] = False):
    """Archive the live ap-quarantine and ap-desk rooms (reversible). Run it BEFORE importing the template again,
    or it will archive the new rooms too."""
    from airlock import rooms as rm

    names = Names.from_env()
    try:
        agent = load_agent(names.customs)
    except FileNotFoundError:
        typer.echo("The protocol agents aren't registered yet: uv run airlock reg-key && uv run airlock register")
        raise typer.Exit(1)
    try:
        rooms = rm.live_desk_rooms(agent.call("list_rooms"), names)
    except (HttpError, OSError) as e:
        typer.echo(f"The server refused customs: {e}. Is the local server running?")
        raise typer.Exit(1)
    if not rooms:
        typer.echo(f"No live {names.quarantine} or {names.desk} room. Nothing to do.")
        return
    typer.echo("Will archive in Switch (reversible, messages kept):")
    for r in rooms:
        typer.echo(f"  {r['name']}  {r['room_id']}")
    typer.echo("Archive the same channels in Mattermost yourself so they leave the sidebar.")
    if not yes and not typer.confirm("Archive these rooms?"):
        raise typer.Exit(1)
    failed = 0
    for o in rm.archive(agent.call, rooms):
        failed += bool(o.error)
        typer.echo(f"{'FAILED ' if o.error else 'archived'} {o.name}" + (f": {o.error}" if o.error else ""))
    raise typer.Exit(1 if failed else 0)


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


Only = Annotated[list[str] | None, typer.Option(help="Corpus ids or prefixes, e.g. a07 or c01-acme.")]
Corpus = Annotated[Path, typer.Option(help="Corpus folder (a manifest.json plus one .eml per case).")]


def _finish_eval(rows: list[dict]) -> None:
    from airlock.eval import report
    for r in rows:
        mark = "ok  " if r["correct"] else "BAD "
        typer.echo(f"{mark}{r['id']:<28} {', '.join(r['harms']) or '-':<36} {r['outcome']}")
    path = report.save(rows)
    report.rebuild()
    typer.echo(f"\n{report.line(rows)}\nraw log: {path}\nsummary: {report.SUMMARY}")


@eval_app.command("airlock")
def eval_airlock(reader: Annotated[str, typer.Option(help="quiet or raw (a hijacked reader, free) or openai")] = "quiet",
                 only: Only = None, corpus: Corpus = Path("eval/corpus")):
    """Run each corpus email through mailroom, reader, customs, the clerk's ERP and the bank."""
    from airlock.eval import airlock_run, corpus as corpus_mod
    items = corpus_mod.load(corpus, only)
    extractor = reader_agent.make_extractor("openai", Path()) if reader == "openai" else None
    if reader not in ("quiet", "raw", "openai"):
        raise typer.BadParameter("reader must be quiet, raw or openai")
    if extractor:
        typer.echo(f"real reader: {extractor.model}, {len(items)} calls")
    _finish_eval([airlock_run.run_item(i, reader, extractor) for i in items])


@eval_app.command("naive")
def eval_naive(prompt: Annotated[str, typer.Option(help="naive or guarded")] = "guarded",
               model: Annotated[str, typer.Option(help="OpenAI model id (default: the reader's model).")] = "",
               only: Only = None, corpus: Corpus = Path("eval/corpus")):
    """Give each corpus email to one agent that holds the AP tools itself. Tools only record; nothing runs."""
    from airlock.eval import corpus as corpus_mod
    from airlock.eval import naive
    if prompt not in naive.PROMPTS:
        raise typer.BadParameter("prompt must be naive or guarded")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        typer.echo("OPENAI_API_KEY is not set")
        raise typer.Exit(1)
    model = model or os.environ.get("AIRLOCK_READER_MODEL", reader_agent.DEFAULT_MODEL)
    items = corpus_mod.load(corpus, only)
    typer.echo(f"{prompt} agent on {model}: {len(items)} emails, at most {naive.MAX_STEPS} calls each")
    chat = naive.OpenAIChat(key)
    rows = []
    for i in items:
        rows.append(naive.run_item(i, prompt, model, chat))
    tokens = sum(r["usage"]["prompt_tokens"] + r["usage"]["completion_tokens"] for r in rows)
    typer.echo(f"tokens used: {tokens}")
    _finish_eval(rows)


@eval_app.command("report")
def eval_report():
    """Rebuild eval/RESULTS.md from the newest run of each setup."""
    from airlock.eval import report
    typer.echo(f"wrote {report.rebuild()}")


if __name__ == "__main__":
    app()
