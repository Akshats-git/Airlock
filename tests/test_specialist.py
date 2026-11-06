"""`airlock specialist`: the second person gets their own Switch user, and is never linked to someone else's."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from airlock import gateway


class _Server(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body=None, cookie=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self):
        return "session=ok" in (self.headers.get("Cookie") or "")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        s = self.state
        if self.path == "/gateway/auth/login":
            return self._send(200, {}, cookie="session=ok; Path=/")
        if not self._authed():
            return self._send(401, {"detail": "no session"})
        if self.path == "/gateway/users":
            s["passwords"].append(body["password"])
            user = {"id": f"u{len(s['users']) + 1}", "name": body["name"], "email": body["email"], "role": body["role"]}
            s["users"].append(user)
            return self._send(200, user)
        if self.path == "/gateway/collaborations/mm1/identities":
            s["claims"].append(body)
            return self._send(200, {"id": "x"})
        self._send(404, {"detail": "nope"})

    def do_GET(self):
        s = self.state
        if not self._authed():
            return self._send(401, {"detail": "no session"})
        url = urllib.parse.urlparse(self.path)
        if url.path == "/gateway/collaborations":
            return self._send(200, [{"bridge_id": "tg", "bridge_type": "telegram", "is_default": True},
                                    {"bridge_id": "mm1", "bridge_type": "mattermost", "is_default": False}])
        if url.path == "/gateway/collaborations/mm1/directory":
            q = urllib.parse.parse_qs(url.query)["query"][0]
            return self._send(200, {"source": "directory", "users": [u for u in s["directory"] if q in u["username"]]})
        if url.path == "/gateway/users":
            return self._send(200, s["users"])
        self._send(404, {"detail": "nope"})


@pytest.fixture
def gw():
    srv = HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Server.state = dict(users=[{"id": "admin", "name": "Admin", "email": "admin@x", "role": "admin"}], claims=[],
                         passwords=[], directory=[
                             {"external_user_id": "mm-sam", "username": "sam", "display_name": "Sam", "claimed_by": []},
                             {"external_user_id": "mm-akshat", "username": "akshat", "display_name": "Akshat",
                              "claimed_by": [{"user_id": "admin", "user_name": "Admin"}]}])
    api = f"http://127.0.0.1:{srv.server_port}"
    yield api, gateway.login(api, "admin@x", "pw")
    srv.shutdown()


def test_creates_a_user_and_links_the_account(gw):
    msg = gateway.link_specialist(*gw, "@sam")
    s = _Server.state
    assert s["users"][-1]["name"] == "AP specialist" and s["users"][-1]["role"] == "user"
    assert s["claims"] == [{"external_user_id": "mm-sam", "username": "sam", "user_id": s["users"][-1]["id"]}]
    assert "Created" in msg and s["passwords"][0] not in msg and len(s["passwords"][0]) >= 24


def test_running_it_twice_changes_nothing(gw):
    gateway.link_specialist(*gw, "sam")
    s = _Server.state
    s["directory"][0]["claimed_by"] = [{"user_id": s["users"][-1]["id"], "user_name": "AP specialist"}]
    assert "nothing to do" in gateway.link_specialist(*gw, "sam")
    assert len(s["users"]) == 2 and len(s["claims"]) == 1


def test_refuses_an_account_already_linked_to_someone_else(gw):
    with pytest.raises(gateway.GatewayError, match="already linked to Admin"):
        gateway.link_specialist(*gw, "akshat")  # the owner's own account: linking it would make it an owner
    assert _Server.state["claims"] == [] and len(_Server.state["users"]) == 1


def test_an_unknown_account_says_how_to_fix_it(gw):
    with pytest.raises(gateway.GatewayError, match="no Mattermost account named 'nobody'"):
        gateway.link_specialist(*gw, "nobody")
