"""Get a registration key for the protocol agents from your own Switch server.

A Console-managed local server has no Gateway web page, so the key comes from the same REST routes that
page would use: sign in as the server's admin, then reuse the key labelled `airlock` or create it. The key
is written to a file only you can read and is never printed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LABEL = "airlock"


class GatewayError(RuntimeError):
    pass


def _req(method: str, url: str, body: dict | None = None, cookie: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    path = urllib.parse.urlparse(url).path
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return (json.loads(raw) if raw else None), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise GatewayError(f"{method} {path}: HTTP {e.code} {detail}") from None
    except urllib.error.URLError as e:
        raise GatewayError(f"{method} {path}: {e.reason}") from None


def login(api: str, email: str, password: str) -> str:
    """Sign in to the Gateway API. Returns the Cookie header for the session."""
    _, cookies = _req("POST", f"{api}/gateway/auth/login", {"email": email, "password": password})
    pairs = [c.split(";", 1)[0] for c in cookies]
    if not pairs:
        raise GatewayError("the sign-in returned no session cookie")
    return "; ".join(pairs)


def registration_key(api: str, cookie: str, label: str = LABEL) -> tuple[str, bool]:
    """The registration key labelled ``label``: revealed if it exists, else created. Returns (key, created)."""
    keys, _ = _req("GET", f"{api}/gateway/api-keys", cookie=cookie)
    for k in keys or []:
        if k.get("label") == label and k.get("type") == "registration":
            res, _ = _req("GET", f"{api}/gateway/api-keys/{k['id']}/reveal", cookie=cookie)
            return res["key"], False
    res, _ = _req("POST", f"{api}/gateway/api-keys", {"label": label}, cookie=cookie)
    return res["key"], True


def key_file(runtime: Path) -> Path:
    return runtime / "registration-key"


def save_key(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def load_key(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None
