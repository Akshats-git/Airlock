"""Find the local Switch server and the folders Console keeps the agents in, on any machine.

Console picks the server's ports when it installs it, so nothing here may assume a port. Order of
precedence: an explicit environment variable, then what Console wrote to disk, then a clear error.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse


class NotFound(RuntimeError):
    """The server or the agents' folders could not be located. The message says what to set."""


def console_dir(home: Path | None = None, platform: str | None = None) -> Path:
    """Where Switch Console keeps its settings (Electron's userData folder).

    $APPDATA / $XDG_CONFIG_HOME count only for the real home folder, never for an explicit ``home``.
    """
    use_env = home is None
    home = home or Path.home()
    platform = platform or sys.platform
    if platform == "darwin":
        return home / "Library" / "Application Support" / "switchdash"
    if platform.startswith("win"):
        appdata = os.environ.get("APPDATA") if use_env else None
        return Path(appdata or home / "AppData" / "Roaming") / "switchdash"
    xdg = os.environ.get("XDG_CONFIG_HOME") if use_env else None
    return Path(xdg or home / ".config") / "switchdash"


def ports_file(home: Path | None = None, platform: str | None = None) -> Path:
    return console_dir(home, platform) / "local-switch-server" / "ports.json"


def api_url(env: dict[str, str] | None = None, home: Path | None = None, platform: str | None = None) -> str:
    """The Switch API (the agent bridge): $AIRLOCK_API, else the port Console chose for the local server."""
    env = os.environ if env is None else env
    if env.get("AIRLOCK_API"):
        return env["AIRLOCK_API"].rstrip("/")
    path = ports_file(home, platform)
    try:
        port = int(json.loads(path.read_text())["api"])
    except (OSError, ValueError, KeyError, TypeError):
        raise NotFound(f"Can't find the Switch server: no API port in {path}. Start the local server in Switch "
                       f"Console, or set AIRLOCK_API (for example http://localhost:43381).") from None
    return f"http://localhost:{port}"


def agents_dir(env: dict[str, str] | None = None, home: Path | None = None, api: str | None = None,
               clerk: str = "ap-clerk", treasury: str = "treasury") -> Path:
    """The folder that holds the clerk's and treasury's folders: $AIRLOCK_AGENTS_DIR, else the one under
    ~/.switch/agents that contains both (Console creates them when the template runs)."""
    env = os.environ if env is None else env
    if env.get("AIRLOCK_AGENTS_DIR"):
        return Path(env["AIRLOCK_AGENTS_DIR"]).expanduser()
    root = (home or Path.home()) / ".switch" / "agents"
    found = sorted(d for d in root.glob("*/") if (d / clerk).is_dir() and (d / treasury).is_dir()) \
        if root.is_dir() else []
    if len(found) == 1:
        return found[0]
    if len(found) > 1 and api:
        u = urlparse(api)
        preferred = root / f"{u.hostname}-{u.port}"
        if preferred in found:
            return preferred
    if not found:
        raise NotFound(f"No folder under {root} holds both {clerk}/ and {treasury}/. Run the Airlock template "
                       f"in Console first, or set AIRLOCK_AGENTS_DIR.")
    raise NotFound(f"Several folders under {root} hold {clerk}/ and {treasury}/: "
                   f"{', '.join(d.name for d in found)}. Set AIRLOCK_AGENTS_DIR to the right one.")


def data_paths(agents: Path, clerk: str = "ap-clerk", treasury: str = "treasury") -> dict[str, Path]:
    """Where the ERP, the bank and the receipts inbox live once `airlock bootstrap` has run."""
    return {
        "AIRLOCK_ERP": agents / clerk / "erp.sqlite",
        "AIRLOCK_BANK": agents / treasury / "bank.sqlite",
        "AIRLOCK_RECEIPTS": agents / clerk / "inbox" / "receipts.jsonl",
    }
