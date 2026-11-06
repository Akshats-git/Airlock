"""Get a registration key for the protocol agents from your own Switch server.

A Console-managed local server has no Gateway web page, so the key comes from the same REST routes that
page would use: sign in as the server's admin, then reuse the key labelled `airlock` or create it. The key
is written to a file only you can read and is never printed.
"""

from __future__ import annotations

import json
import os
import secrets
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


SPECIALIST_NAME = "AP specialist"


def specialist_email(username: str) -> str:
    return f"ap-specialist+{username.lower()}@airlock.example"


def mattermost_bridge(api: str, cookie: str) -> str:
    bridges, _ = _req("GET", f"{api}/gateway/collaborations", cookie=cookie)
    mm = [b for b in bridges or [] if "mattermost" in str(b.get("bridge_type", "")).lower()]
    if not mm:
        raise GatewayError("this server has no Mattermost bridge")
    mm.sort(key=lambda b: not b.get("is_default"))
    return mm[0]["bridge_id"]


def link_specialist(api: str, cookie: str, username: str) -> str:
    """Give a second chat account its own Switch user, so Switch knows it as a person who is not the owner.

    Switch then refuses that person by name wherever only the owner may act (an @-mention of treasury, an
    answer on treasury's approval card). Without a linked user Switch can't tell who the account is, and its
    refusal asks for the account to be linked instead. Returns a one-line report. Safe to run twice.
    """
    username = username.strip().lstrip("@")
    bridge = mattermost_bridge(api, cookie)
    q = urllib.parse.urlencode({"query": username})
    found, _ = _req("GET", f"{api}/gateway/collaborations/{bridge}/directory?{q}", cookie=cookie)
    person = next((u for u in (found or {}).get("users", []) if u["username"].lower() == username.lower()), None)
    if person is None:
        raise GatewayError(f"no Mattermost account named {username!r}; create it first (see the README)")

    users, _ = _req("GET", f"{api}/gateway/users", cookie=cookie)
    email = specialist_email(username)
    me = next((u for u in users or [] if u["email"].lower() == email), None)
    claimants = person.get("claimed_by") or []
    others = [c for c in claimants if not me or c["user_id"] != me["id"]]
    if others:
        names = ", ".join(c["user_name"] for c in others)
        raise GatewayError(f"{username} is already linked to {names}; Switch would treat them as that user. "
                           "Use an account nobody has linked.")
    if me and claimants:
        return f"{username} is already linked to the Switch user {me['name']!r}; nothing to do"
    created = me is None
    if created:
        # Nobody signs in as this user: the specialist only ever acts through chat. The password is random and
        # is never shown or stored.
        me, _ = _req("POST", f"{api}/gateway/users", {"name": SPECIALIST_NAME, "email": email,
                                                     "password": secrets.token_urlsafe(24), "role": "user"},
                     cookie=cookie)
    _req("POST", f"{api}/gateway/collaborations/{bridge}/identities",
         {"external_user_id": person["external_user_id"], "username": person["username"], "user_id": me["id"]},
         cookie=cookie)
    return (f"{'Created' if created else 'Reused'} the Switch user {me['name']!r} ({email}) and linked "
            f"{person['username']} to it")
