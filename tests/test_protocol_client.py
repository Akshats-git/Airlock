"""The protocol client against a scripted fake socket and a stub HTTP server."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from airlock.protocol import Credentials, HttpError, ProtocolError, Stopped, SwitchAgent, load_or_register
from airlock.protocol import client as client_mod

CREDS = Credentials("agent-1", "key-1", "conn-1")


def state(gen=7, cursor=0):
    return {"event": "connection_state", "data": {"generation": gen, "cursor": cursor}}


def ping():
    return {"event": "ping", "data": {}}


def msg(seq, body="hi", addressed=True, room="R"):
    p = {"addressed": addressed, "sender": "@u", "sender_name": "akshat", "message_id": f"m{seq}",
         "body": body, "thread_id": None, "attachments": []}
    return {"event": "message", "id": seq, "data": {"type": "message", "room_id": room,
                                                    "payload": p, "sequence": seq}}


def closed(code):
    return ConnectionClosed(Close(code, ""), None)


class FakeWS:
    def __init__(self, script):
        self.script, self.sent = list(script), []

    async def recv(self):
        while not self.script:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        await asyncio.sleep(0)
        return json.dumps(item)

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConnect:
    """One script per connection attempt. Records each URL."""

    def __init__(self, *scripts):
        self.scripts, self.urls, self.sockets = list(scripts), [], []

    def __call__(self, url, additional_headers=None):
        self.urls.append(url)
        ws = FakeWS(self.scripts.pop(0))
        self.sockets.append(ws)
        return ws


def drive(agent, handler, timeout=5, stop_when=None):
    async def go():
        stop = asyncio.Event()
        got = []

        async def wrapped(m):
            got.append(m)
            await handler(m)
            if stop_when and stop_when(got):
                stop.set()

        await asyncio.wait_for(agent.run(wrapped, stop), timeout)
        return got

    return asyncio.run(go())


async def noop(_m):
    return None


def test_pong_is_sent_while_a_slow_handler_is_still_running():
    connect = FakeConnect([state(), msg(100), ping(), ping()])
    agent = SwitchAgent(CREDS, connect=connect)
    release = asyncio.Event
    seen = {}

    async def go():
        rel = release()
        stop = asyncio.Event()

        async def slow(m):
            await rel.wait()  # blocked until the pongs are out

        task = asyncio.create_task(agent.run(slow, stop))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(connect.sockets) and len(connect.sockets[0].sent) >= 2:
                break
        seen["pongs"] = list(connect.sockets[0].sent)
        seen["handler_done"] = rel.is_set()
        rel.set()
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert seen["handler_done"] is False
    assert seen["pongs"] == [{"type": "pong", "cursor": 100}] * 2


def test_resumes_from_cursor_and_generation_after_a_drop():
    connect = FakeConnect([state(gen=5), msg(100), closed(1012)], [state(gen=5, cursor=100), msg(101)])
    agent = SwitchAgent(CREDS, rooms=("R",), connect=connect, max_backoff=0.01)
    got = drive(agent, noop, stop_when=lambda g: len(g) == 2)
    assert [m.sequence for m in got] == [100, 101]
    q1 = parse_qs(urlparse(connect.urls[0]).query)
    q2 = parse_qs(urlparse(connect.urls[1]).query)
    assert "start_from" not in q1 and q1["rooms"] == ["R"]
    assert q2["start_from"] == ["100"] and q2["expected_generation"] == ["5"]
    assert q2["connection_id"] == ["conn-1"]


def test_taken_over_is_final_and_does_not_reconnect():
    connect = FakeConnect([state(), {"event": "evicted", "data": {"code": "taken_over", "reason": "x"}}])
    with pytest.raises(Stopped):
        drive(SwitchAgent(CREDS, connect=connect), noop)
    assert len(connect.urls) == 1


def test_heartbeat_lapse_reconnects():
    connect = FakeConnect([state(), msg(10), {"event": "evicted", "data": {"code": "heartbeat_lapsed"}}],
                          [state(cursor=10), msg(11)])
    agent = SwitchAgent(CREDS, connect=connect, max_backoff=0.01)
    got = drive(agent, noop, stop_when=lambda g: len(g) == 2)
    assert len(connect.urls) == 2 and [m.sequence for m in got] == [10, 11]


@pytest.mark.parametrize("script", [
    [closed(4409)],
    [closed(4401)],
    [{"event": "refused", "data": {"status": 409, "detail": "room already claimed"}}],
    [{"event": "evicted", "data": {"code": "closed", "reason": "agent deleted"}}],
])
def test_refusals_and_final_closes_stop_the_client(script):
    connect = FakeConnect(script)
    with pytest.raises(Stopped):
        drive(SwitchAgent(CREDS, connect=connect), noop)
    assert len(connect.urls) == 1


def test_only_addressed_messages_reach_the_handler_by_default():
    connect = FakeConnect([state(), msg(1, "a", addressed=False), msg(2, "b"), msg(3, "c", addressed=False)])
    got = drive(SwitchAgent(CREDS, connect=connect), noop, stop_when=lambda g: len(g) == 1)
    assert [m.body for m in got] == ["b"]


def test_can_deliver_everything_when_asked():
    connect = FakeConnect([state(), msg(1, "a", addressed=False), msg(2, "b")])
    agent = SwitchAgent(CREDS, connect=connect, only_addressed=False)
    got = drive(agent, noop, stop_when=lambda g: len(g) == 2)
    assert [(m.body, m.addressed) for m in got] == [("a", False), ("b", True)]


def test_a_failing_handler_does_not_kill_the_connection():
    connect = FakeConnect([state(), msg(1), msg(2)])

    async def boom(m):
        if m.sequence == 1:
            raise RuntimeError("bad handler")

    got = drive(SwitchAgent(CREDS, connect=connect), boom, stop_when=lambda g: len(g) == 2)
    assert [m.sequence for m in got] == [1, 2]


def test_gap_moves_the_cursor_forward():
    connect = FakeConnect([state(), {"event": "gap", "data": {"resumed_at": 500, "reason": "x"}}, ping()])
    agent = SwitchAgent(CREDS, connect=connect)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(agent.run(noop, stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert connect.sockets[0].sent == [{"type": "pong", "cursor": 500}]


def test_credentials_round_trip_and_are_private(tmp_path):
    p = tmp_path / "a" / "x.json"
    CREDS.save(p)
    assert Credentials.load(p) == CREDS
    assert p.stat().st_mode & 0o777 == 0o600


def test_load_or_register_never_registers_twice(tmp_path, monkeypatch):
    p = tmp_path / "x.json"
    CREDS.save(p)
    monkeypatch.setattr(client_mod, "register", lambda *a, **k: pytest.fail("registered again"))
    assert load_or_register(p, "n", "d", "reg-key") == CREDS
    with pytest.raises(ProtocolError):
        load_or_register(tmp_path / "missing.json", "n", "d", None)


class _Stub(BaseHTTPRequestHandler):
    seen: list = []

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null")
        _Stub.seen.append((self.command, self.path, dict(self.headers), body))
        if self.path.endswith("/forbidden"):
            out, code = {"detail": "nope"}, 403
        elif self.path.endswith("/message"):
            out, code = {"ok": True, "event_id": "ev-1"}, 200
        else:
            out, code = {"result": {"echo": body}}, 200
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = _go

    def log_message(self, *a):
        pass


@pytest.fixture
def stub():
    _Stub.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_post_and_call_http_shapes(stub):
    a = SwitchAgent(CREDS, api=stub)
    assert a.post("room-9", "hello") == "ev-1"
    method, path, headers, body = _Stub.seen[-1]
    assert (method, path) == ("POST", "/agents/agent-1/message")
    assert body == {"room_id": "room-9", "content": "hello"}
    assert headers["Authorization"] == "Bearer key-1"

    assert a.call("list_rooms", include_archived=False) == {"echo": {"include_archived": False}}
    assert "X-Switch-Connection-Id" not in _Stub.seen[-1][2]
    a.call("post_message", in_room=True, body="x")
    assert _Stub.seen[-1][2]["X-Switch-Connection-Id"] == "conn-1"
    assert _Stub.seen[-1][1] == "/agents/agent-1/ops/post_message"


def test_http_errors_carry_status_and_detail(stub):
    with pytest.raises(HttpError) as e:
        SwitchAgent(CREDS, api=stub).call("forbidden")
    assert e.value.status == 403 and e.value.detail == "nope"
