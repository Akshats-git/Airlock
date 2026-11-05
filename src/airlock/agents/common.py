"""Settings and helpers shared by the four protocol agents."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from airlock.protocol import Credentials, Message, SwitchAgent, load_or_register
from airlock.protocol.client import DEFAULT_API, Stopped

log = logging.getLogger("airlock.agents")

AGENT_DESCRIPTIONS = {
    "mailroom": "Airlock mailroom: posts each vendor email into quarantine. Plain code, takes no instructions.",
    "reader": "Airlock reader: turns one email into one record. A model with no tools.",
    "customs": "Airlock customs: the schema gate between quarantine and the AP desk. Plain code.",
    "red-team": "Airlock red team: posts scripted attacks on cue so the walls can be shown holding.",
}


@dataclass(frozen=True)
class Names:
    """Every name the agents rely on, in one place. Defaults match the template."""

    mailroom: str = "mailroom"
    reader: str = "reader"
    customs: str = "customs"
    red_team: str = "red-team"
    clerk: str = "ap-clerk"
    treasury: str = "treasury"
    quarantine: str = "ap-quarantine"
    desk: str = "ap-desk"

    @classmethod
    def from_env(cls) -> "Names":
        env = {f: os.environ.get(f"AIRLOCK_NAME_{f.upper()}") for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in env.items() if v})


def same_agent(sender_name: str, expected: str) -> bool:
    """Compare a chat display name with an agent name, ignoring case and a leading @."""
    return sender_name.strip().lstrip("@").casefold() == expected.casefold()


def api_url() -> str:
    return os.environ.get("AIRLOCK_API", DEFAULT_API)


def runtime_dir() -> Path:
    return Path(os.environ.get("AIRLOCK_RUNTIME", "runtime"))


def creds_path(name: str) -> Path:
    return runtime_dir() / "agents" / f"{name}.json"


def load_agent(name: str, **kwargs) -> SwitchAgent:
    """Open the saved registration for ``name``. Never registers: that is `airlock register`."""
    return SwitchAgent(Credentials.load(creds_path(name)), api=api_url(), **kwargs)


def register_agent(name: str, registration_key: str) -> Credentials:
    return load_or_register(creds_path(name), name, AGENT_DESCRIPTIONS[name], registration_key, api=api_url())


def resolve_room(agent: SwitchAgent, name: str) -> str:
    """Room id by exact name, among the rooms this agent belongs to."""
    rooms = [r for r in agent.call("list_rooms") if r.get("name") == name and not r.get("archived")]
    if not rooms:
        raise LookupError(f"{agent.creds.id} is in no room named {name!r}; run the Airlock template and "
                          f"invite the agent first")
    if len(rooms) > 1:
        raise LookupError(f"more than one room named {name!r}; archive the old ones")
    return rooms[0]["room_id"]


async def apost(agent: SwitchAgent, room_id: str, text: str) -> str:
    """post() is blocking HTTP; keep it off the event loop so pongs are never delayed."""
    return await asyncio.to_thread(agent.post, room_id, text)


def new_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%H%M%S")


async def serve(agent: SwitchAgent, on_message, *, banner: str, extra=None) -> None:
    """Hold the connection until Ctrl-C or SIGTERM. ``extra`` is an optional background coroutine."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info(banner)
    tasks = [asyncio.create_task(agent.run(on_message, stop))]
    if extra is not None:
        tasks.append(asyncio.create_task(extra(stop)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.result()
    except Stopped as why:
        log.error("the server ended this connection for good: %s", why)
        raise SystemExit(2) from None
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["Names", "Message", "same_agent", "load_agent", "register_agent", "resolve_room", "apost",
           "new_run_id", "serve", "creds_path", "runtime_dir", "AGENT_DESCRIPTIONS"]
