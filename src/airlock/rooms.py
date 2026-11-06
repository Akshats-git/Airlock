"""Retire the desk's old rooms before a fresh take.

Console shows "Delete room" only to a room's owner or an admin, and Switch does not notice a channel archived
in the chat app. Switch's own `archive_room` operation (reversible, keeps every message) does the job, and
customs is a member of both rooms, so it can call it. Only rooms named like the desk's two are touched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from airlock.agents.common import Names


@dataclass(frozen=True)
class Outcome:
    name: str
    room_id: str
    error: str = ""


def live_desk_rooms(rooms: list[dict], names: Names) -> list[dict]:
    """The rooms that would be archived: live ones named like the quarantine or the desk."""
    wanted = {names.quarantine, names.desk}
    return [r for r in rooms if r.get("name") in wanted and not r.get("archived")]


def archive(call: Callable[..., dict], rooms: list[dict]) -> list[Outcome]:
    """Archive each room. One refusal (for example customs isn't a member) doesn't stop the others."""
    out: list[Outcome] = []
    for r in rooms:
        try:
            call("archive_room", room_id=r["room_id"])
            out.append(Outcome(r["name"], r["room_id"]))
        except Exception as e:  # report, keep going
            out.append(Outcome(r["name"], r["room_id"], str(e)))
    return out
