"""Retire the desk's old rooms before a fresh take.

Console shows "Delete room" only to a room's owner or an admin, and Switch does not notice a channel archived
in the chat app. Switch's own `archive_room` operation (reversible, keeps every message) does the job, and
customs is a member of both rooms, so it can call it. Only rooms named like the desk's two are touched.
"""

from __future__ import annotations

import subprocess
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


def channel_ids(call: Callable[..., dict], rooms: list[dict]) -> dict[str, str]:
    """Room id -> the chat app's channel id, read from Switch before the room is archived."""
    out: dict[str, str] = {}
    for r in rooms:
        try:
            cid = call("get_room_detail", room_id=r["room_id"]).get("external_channel_id")
        except Exception:
            cid = None
        if cid:
            out[r["room_id"]] = cid
    return out


def mattermost_container(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str | None:
    """The name of the running bundled Mattermost container, found by its image."""
    try:
        p = run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in p.stdout.splitlines():
        name, _, image = line.partition("\t")
        if image.startswith("mattermost/mattermost"):
            return name
    return None


def archive_channels(container: str, channel_ids: list[str], team: str = "switch",
                     run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[Outcome]:
    """Archive the channels in the bundled Mattermost (mmctl in local mode), so they leave the sidebar."""
    out: list[Outcome] = []
    for cid in channel_ids:
        p = run(["docker", "exec", container, "mmctl", "--local", "channel", "archive", f"{team}:{cid}"],
                capture_output=True, text=True, timeout=60)
        out.append(Outcome("mattermost channel", cid, "" if p.returncode == 0 else (p.stderr or p.stdout).strip()))
    return out
