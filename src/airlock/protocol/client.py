"""A small Switch agent-protocol client (see 02-switch-docs/.../agent-protocol.md).

Register once, hold one WebSocket, answer every ping at once with a pong that carries
the cursor, act only on messages addressed to the agent, and resume after a drop.
Everything the agent does besides listening is plain HTTP.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

# Only a fallback for direct library use; the `airlock` command finds the real port (airlock.local).
DEFAULT_API = "http://localhost:43381"
log = logging.getLogger("airlock.protocol")

# `evicted` codes after which reopening and resuming is right; `taken_over` and `closed` are final.
_RESUMABLE = {"heartbeat_lapsed", "no_stream", "unfenced", "connection_error"}
_DOMAIN_EVENTS = {"message", "command", "room_join"}


class ProtocolError(RuntimeError):
    pass


class HttpError(ProtocolError):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


class Stopped(ProtocolError):
    """The server ended the connection for good. Do not reconnect."""


class _Reconnect(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    id: str
    api_key: str
    connection_id: str

    @classmethod
    def load(cls, path: Path) -> "Credentials":
        d = json.loads(Path(path).read_text())
        return cls(d["id"], d["api_key"], d["connection_id"])

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": self.id, "api_key": self.api_key,
                                    "connection_id": self.connection_id}))
        path.chmod(0o600)


@dataclass(frozen=True)
class Message:
    room_id: str
    sender: str
    sender_name: str
    body: str
    message_id: str
    thread_id: str | None
    addressed: bool
    sequence: int
    attachments: tuple = ()


def _http(method: str, url: str, body: Any = None, key: str | None = None,
          conn: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if conn:
        headers["X-Switch-Connection-Id"] = conn
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except (ValueError, AttributeError):
            detail = raw
        raise HttpError(e.code, detail) from None


def register(name: str, description: str, registration_key: str, *, api: str = DEFAULT_API,
             connection_model: str = "always_on") -> Credentials:
    """Register a new agent. Registering a name that exists is a 409 (HttpError)."""
    body = {
        "name": name, "description": description, "connector_type": "airlock",
        "integration_profile": {
            "connection_model": connection_model, "message_exchange": True,
            "pre_invocation_mediation": [], "post_invocation_mediation": [], "event_reporting": [],
        },
    }
    res = _http("POST", f"{api}/agents", body, key=registration_key)
    return Credentials(res["id"], res["api_key"], str(uuid.uuid4()))


def load_or_register(path: Path, name: str, description: str, registration_key: str | None = None,
                     *, api: str = DEFAULT_API) -> Credentials:
    """Reuse the saved registration; only register (once) when there is none."""
    path = Path(path)
    if path.exists():
        return Credentials.load(path)
    if not registration_key:
        raise ProtocolError(f"{path} is missing and no registration key was given")
    creds = register(name, description, registration_key, api=api)
    creds.save(path)
    return creds


MessageHandler = Callable[[Message], Awaitable[None]]


class SwitchAgent:
    def __init__(self, creds: Credentials, *, api: str = DEFAULT_API, rooms: tuple[str, ...] = (),
                 scope: str = "all", filter: str = "all", only_addressed: bool = True,
                 connect: Callable[..., Any] | None = None, max_backoff: float = 10.0):
        self.creds, self.api = creds, api.rstrip("/")
        self.rooms, self.scope, self.filter = rooms, scope, filter
        self.only_addressed = only_addressed
        self.cursor = 0
        self.generation: int | None = None
        self.connected = False
        self._connect = connect or websockets.connect
        self._max_backoff = max_backoff

    # ---- plain HTTP: no socket needed -------------------------------------------------
    def post(self, room_id: str, text: str) -> str:
        """Post to a room via /message. Returns the event id."""
        res = _http("POST", f"{self.api}/agents/{self.creds.id}/message",
                    {"room_id": room_id, "content": text}, key=self.creds.api_key)
        return res["event_id"]

    def call(self, op: str, *, in_room: bool = False, **args: Any) -> Any:
        """Call an operation. in_room=True binds it to the open connection's room."""
        conn = self.creds.connection_id if in_room else None
        res = _http("POST", f"{self.api}/agents/{self.creds.id}/ops/{op}", args,
                    key=self.creds.api_key, conn=conn)
        return res["result"]

    # ---- the socket -------------------------------------------------------------------
    def _url(self) -> str:
        q = {"connection_id": self.creds.connection_id, "scope": self.scope, "filter": self.filter,
             "client": "airlock", "client_version": "0"}
        if self.rooms:
            q["rooms"] = ",".join(self.rooms)
        if self.cursor:
            q["start_from"] = str(self.cursor)
            if self.generation is not None:
                q["expected_generation"] = str(self.generation)
        base = self.api.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/agents/{self.creds.id}/connection/ws?{urllib.parse.urlencode(q)}"

    def _handle(self, frame: dict) -> Message | None:
        """Digest one frame. Returns a Message to act on, or None."""
        kind = frame.get("event") or frame.get("type")
        data = frame.get("data", frame)
        if kind == "connection_state":
            self.generation = data.get("generation", self.generation)
            self.cursor = max(self.cursor, data.get("cursor") or 0)
        elif kind == "gap":
            self.cursor = max(self.cursor, data.get("resumed_at") or 0)
            log.warning("gap in events: %s", data.get("reason"))
        elif kind == "evicted":
            code = data.get("code")
            if code in _RESUMABLE:
                raise _Reconnect(code)
            raise Stopped(f"evicted: {code} ({data.get('reason')})")
        elif kind == "refused":
            raise Stopped(f"refused: {data.get('status')} {data.get('detail')}")
        elif kind in _DOMAIN_EVENTS:
            seq = frame.get("id", data.get("sequence", 0))
            self.cursor = max(self.cursor, seq)
            p = data.get("payload", {})
            if kind == "message" and (p.get("addressed") or not self.only_addressed):
                return Message(
                    room_id=data.get("room_id", ""), sender=p.get("sender", ""),
                    sender_name=p.get("sender_name", ""), body=p.get("body", ""),
                    message_id=p.get("message_id", ""), thread_id=p.get("thread_id"),
                    addressed=bool(p.get("addressed")), sequence=seq,
                    attachments=tuple(p.get("attachments") or ()))
        return None

    async def _session(self, on_message: MessageHandler, tasks: set, stop: asyncio.Event) -> None:
        headers = {"Authorization": f"Bearer {self.creds.api_key}"}
        try:
            async with self._connect(self._url(), additional_headers=headers) as ws:
                self.connected = True
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    frame = json.loads(raw)
                    if (frame.get("event") or frame.get("type")) == "ping":
                        # Pong first, before any event work, so a slow handler never lapses us.
                        await ws.send(json.dumps({"type": "pong", "cursor": self.cursor}))
                        continue
                    msg = self._handle(frame)
                    if msg is not None:
                        t = asyncio.create_task(self._safe(on_message, msg))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            if code is not None and 4400 <= code <= 4499:
                raise Stopped(f"server closed the socket with {code}") from None
            raise _Reconnect(f"closed {code}") from None
        finally:
            self.connected = False

    @staticmethod
    async def _safe(on_message: MessageHandler, msg: Message) -> None:
        try:
            await on_message(msg)
        except Exception:
            log.exception("handler failed for message %s", msg.message_id)

    async def run(self, on_message: MessageHandler, stop: asyncio.Event | None = None) -> None:
        """Hold the connection until `stop` is set. Raises Stopped when the server ends it."""
        stop = stop or asyncio.Event()
        tasks: set[asyncio.Task] = set()
        delay = 0.5
        try:
            while not stop.is_set():
                try:
                    await self._session(on_message, tasks, stop)
                    delay = 0.5
                except (_Reconnect, OSError) as why:
                    log.info("reconnecting from cursor %s (%s)", self.cursor, why)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._max_backoff)
        finally:
            for t in tasks:
                t.cancel()
