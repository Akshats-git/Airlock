"""The setup tools a stranger runs first: finding the server, getting a key, and `airlock doctor`."""

from __future__ import annotations

import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from airlock import doctor as dr
from airlock import gateway, local
from airlock.agents.common import Names

N = Names()


# ---- finding the server and the folders -----------------------------------------------------------

def test_api_url_prefers_env(tmp_path):
    assert local.api_url({"AIRLOCK_API": "http://h:1/"}, home=tmp_path) == "http://h:1"


def test_api_url_reads_the_port_console_chose(tmp_path):
    f = local.ports_file(tmp_path, "linux")
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"gateway": 1, "api": 51234, "mattermost": 2}))
    assert local.api_url({}, home=tmp_path, platform="linux") == "http://localhost:51234"


def test_api_url_says_what_to_set_when_nothing_is_found(tmp_path):
    with pytest.raises(local.NotFound, match="AIRLOCK_API"):
        local.api_url({}, home=tmp_path, platform="linux")


def test_an_explicit_home_ignores_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))  # GitHub's runners set it
    assert local.console_dir(tmp_path, "linux") == tmp_path / ".config" / "switchdash"
    with pytest.raises(local.NotFound):
        local.api_url({}, home=tmp_path, platform="linux")


def test_console_dir_per_platform(tmp_path):
    assert local.console_dir(tmp_path, "darwin") == tmp_path / "Library/Application Support/switchdash"
    assert local.console_dir(tmp_path, "linux").name == "switchdash"


def _agents(tmp_path, *servers):
    for s in servers:
        for a in ("ap-clerk", "treasury"):
            (tmp_path / ".switch/agents" / s / a).mkdir(parents=True)


def test_agents_dir_is_the_one_holding_clerk_and_treasury(tmp_path):
    _agents(tmp_path, "localhost-50000")
    (tmp_path / ".switch/agents/other/ap-clerk").mkdir(parents=True)  # no treasury: not a match
    assert local.agents_dir({}, home=tmp_path).name == "localhost-50000"


def test_agents_dir_picks_the_current_server_when_several_match(tmp_path):
    _agents(tmp_path, "localhost-50000", "localhost-60000")
    assert local.agents_dir({}, home=tmp_path, api="http://localhost:60000").name == "localhost-60000"
    with pytest.raises(local.NotFound, match="Several"):
        local.agents_dir({}, home=tmp_path, api="http://localhost:1")


def test_agents_dir_env_wins_and_missing_is_explained(tmp_path):
    assert local.agents_dir({"AIRLOCK_AGENTS_DIR": str(tmp_path / "x")}, home=tmp_path) == tmp_path / "x"
    with pytest.raises(local.NotFound, match="Run the Airlock template"):
        local.agents_dir({}, home=tmp_path)


# ---- getting a registration key ------------------------------------------------------------------

class _Gateway(BaseHTTPRequestHandler):
    keys: list[dict] = []
    created: list[str] = []

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
        if self.path == "/gateway/auth/login":
            if body == {"email": "admin@x", "password": "right"}:
                return self._send(200, {"email": "admin@x"}, cookie="session=ok; HttpOnly; Path=/")
            return self._send(401, {"detail": "Invalid email or password"})
        if self.path == "/gateway/api-keys" and self._authed():
            self.created.append(body["label"])
            return self._send(200, {"id": "new", "label": body["label"], "key": "NEW-KEY", "created_at": "t"})
        self._send(401, {"detail": "no session"})

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"detail": "no session"})
        if self.path == "/gateway/api-keys":
            return self._send(200, self.keys)
        if self.path == "/gateway/api-keys/k1/reveal":
            return self._send(200, {"key": "OLD-KEY"})
        self._send(404, {"detail": "nope"})


@pytest.fixture
def gw():
    srv = HTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Gateway.keys, _Gateway.created = [], []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_reg_key_reuses_the_airlock_key(gw):
    _Gateway.keys = [{"id": "k0", "label": "airlock", "type": "agent"},
                     {"id": "k1", "label": "airlock", "type": "registration"}]
    assert gateway.registration_key(gw, gateway.login(gw, "admin@x", "right")) == ("OLD-KEY", False)
    assert _Gateway.created == []


def test_reg_key_creates_one_when_there_is_none(gw):
    assert gateway.registration_key(gw, gateway.login(gw, "admin@x", "right")) == ("NEW-KEY", True)
    assert _Gateway.created == ["airlock"]


def test_wrong_password_is_refused_without_echoing_it(gw):
    with pytest.raises(gateway.GatewayError) as e:
        gateway.login(gw, "admin@x", "hunter2-secret")
    assert "401" in str(e.value) and "hunter2-secret" not in str(e.value)


def test_saved_key_is_private(tmp_path):
    p = gateway.key_file(tmp_path / "rt")
    gateway.save_key(p, "K")
    assert gateway.load_key(p) == "K"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert gateway.load_key(tmp_path / "missing") is None


# ---- airlock doctor ------------------------------------------------------------------------------

ROOMS = [{"room_id": "q", "name": "ap-quarantine", "archived": False},
         {"room_id": "d", "name": "ap-desk", "archived": False},
         {"room_id": "old", "name": "ap-desk", "archived": True}]
LIVE = "live"


def _detail(q_agents, d_agents, private=True):
    def detail(rid):
        agents = q_agents if rid == "q" else d_agents
        return {"agent_names": agents, "agent_statuses": {a: LIVE for a in agents},
                "channel_type": "channel_private" if private else "channel_open"}
    return detail


GOOD_Q = ["mailroom", "reader", "customs"]
GOOD_D = ["ap-clerk", "treasury", "customs", "red-team"]


def _statuses(checks):
    return [c.status for c in checks]


def test_doctor_passes_the_template_layout():
    checks = dr.walls(ROOMS, _detail(GOOD_Q, GOOD_D), N)
    assert set(_statuses(checks)) == {dr.OK}


@pytest.mark.parametrize("q, d, needle", [
    (GOOD_Q + ["treasury"], GOOD_D, "treasury can read ap-quarantine"),
    (GOOD_Q + ["ap-clerk"], GOOD_D, "ap-clerk can read ap-quarantine"),
    (GOOD_Q, GOOD_D + ["reader"], "reader is in ap-desk"),
    (["mailroom", "customs"], GOOD_D, "missing reader"),
])
def test_doctor_fails_a_breached_wall(q, d, needle):
    checks = dr.walls(ROOMS, _detail(q, d), N)
    assert any(c.status == dr.FAIL and needle in c.label for c in checks), [c.label for c in checks]


def test_doctor_flags_duplicate_rooms_and_public_channels():
    dup = ROOMS + [{"room_id": "d2", "name": "ap-desk", "archived": False}]
    assert any("2 rooms named ap-desk" in c.label for c in dr.walls(dup, _detail(GOOD_Q, GOOD_D), N))
    public = dr.walls(ROOMS, _detail(GOOD_Q, GOOD_D, private=False), N)
    assert any(c.status == dr.WARN and "not a private channel" in c.label for c in public)


OWNER_ONLY = {"rules": [{"rooms": "*", "owner": True, "owner_agents": False, "users": [], "agents": [],
                         "platform": False}]}


def test_doctor_checks_treasury_obeys_its_owner_only():
    ok = dr.addressing([{"name": "treasury", "addressing_policy": OWNER_ONLY}], N)
    assert _statuses(ok) == [dr.OK]
    loose = json.loads(json.dumps(OWNER_ONLY))
    loose["rules"][0]["owner_agents"] = True  # Switch's default: the owner's own agents may instruct it
    bad = dr.addressing([{"name": "treasury", "addressing_policy": loose}], N)
    assert _statuses(bad) == [dr.FAIL]


def test_doctor_says_whether_the_clerk_is_sandboxed():
    t = {"name": "treasury", "addressing_policy": OWNER_ONLY}
    codex = dr.addressing([t, {"name": "ap-clerk", "connector_type": "Codex"}], N)
    claude = dr.addressing([t, {"name": "ap-clerk", "connector_type": "Claude Code"}], N)
    assert _statuses(codex) == [dr.OK, dr.OK]
    assert _statuses(claude) == [dr.OK, dr.WARN]


def test_doctor_checks_the_agents_files(tmp_path):
    from airlock.bootstrap import install
    assert dr.files(tmp_path, N)[0].status == dr.FAIL
    install(tmp_path)
    checks = dr.files(tmp_path, N)
    assert _statuses(checks) == [dr.OK, dr.OK, dr.OK] and "1,000,000.00" in checks[1].label
    assert "10 verified accounts" in checks[2].label


def test_doctor_report_fails_only_on_failures():
    _, ok = dr.report([dr.Check(dr.OK, "a"), dr.Check(dr.WARN, "b")])
    text, bad = dr.report([dr.Check(dr.FAIL, "c", "do x")])
    assert ok and not bad and "-> do x" in text


# ---- airlock archive-rooms -----------------------------------------------------------------------

def test_only_live_desk_rooms_are_archived():
    from airlock import rooms as rm
    rooms = ROOMS + [{"room_id": "t", "name": "Mattermost: Town Square", "archived": False}]
    assert [r["room_id"] for r in rm.live_desk_rooms(rooms, N)] == ["q", "d"]  # not the archived one, not Town Square


def test_archive_keeps_going_after_a_refusal():
    from airlock import rooms as rm
    seen = []

    def call(op, **kw):
        seen.append((op, kw["room_id"]))
        if kw["room_id"] == "q":
            raise RuntimeError("403 not a member")
        return {}

    out = rm.archive(call, [{"room_id": "q", "name": "ap-quarantine"}, {"room_id": "d", "name": "ap-desk"}])
    assert seen == [("archive_room", "q"), ("archive_room", "d")]
    assert [bool(o.error) for o in out] == [True, False] and "403" in out[0].error


def _people(q_people, d_people):
    def detail(rid):
        return {"connected_user_names": q_people if rid == "q" else d_people}
    return detail


def test_doctor_checks_the_specialists_seat():
    seat = lambda q, d: dr.people(ROOMS, _people(q, d), N, "sam")[0]  # noqa: E731
    assert seat(["akshat"], ["akshat", "sam"]).status == dr.OK
    assert seat(["akshat", "sam"], ["akshat", "sam"]).status == dr.FAIL  # must never read raw vendor mail
    assert seat(["akshat"], ["akshat"]).status == dr.WARN
    unnamed = dr.people(ROOMS, _people(["akshat"], ["akshat", "sam"]), N, None)[0]
    assert unnamed.status == dr.OK and "akshat, sam" in unnamed.label


def test_the_controller_is_the_one_person_in_quarantine(tmp_path):
    from airlock import doctor as dr
    from airlock.agents.common import Names

    names = Names()
    rooms = [{"name": names.quarantine, "room_id": "q", "archived": False},
             {"name": names.quarantine, "room_id": "old", "archived": True}]
    assert dr.controller(rooms, lambda rid: {"connected_user_names": ["akshat"]}, names) == "akshat"
    assert dr.controller(rooms, lambda rid: {"connected_user_names": ["akshat", "sam"]}, names) is None
    assert dr.controller([], lambda rid: {}, names) is None
    (tmp_path / names.treasury / "bin").mkdir(parents=True)
    (tmp_path / names.treasury / "bin" / "bank").write_text('export AIRLOCK_CONTROLLER="akshat"\n')
    assert dr.controller_check(tmp_path, names)[0].status == dr.OK


def test_the_bank_tool_must_name_the_controller_switch_seats(tmp_path):
    from airlock import doctor as dr
    from airlock.agents.common import Names

    names = Names()
    (tmp_path / names.treasury / "bin").mkdir(parents=True)
    (tmp_path / names.treasury / "bin" / "bank").write_text('export AIRLOCK_CONTROLLER="akshat"\n')
    assert dr.controller_check(tmp_path, names, "akshat")[0].status == dr.OK
    assert "approved_by=akshat" in dr.controller_check(tmp_path, names, "akshat")[0].label
    stale = dr.controller_check(tmp_path, names, "maria")[0]
    assert stale.status == dr.FAIL and "akshat" in stale.label and "maria" in stale.label


def test_treasury_must_still_be_owner_only():
    from airlock import doctor as dr
    from airlock.agents.common import Names

    names = Names()
    only = {"rules": [{"owner": True}]}
    open_ = {"rules": [{"owner": True, "owner_agents": True}]}
    assert dr.treasury_owner_only([{"name": names.treasury, "addressing_policy": only}], names).status == dr.OK
    assert dr.treasury_owner_only([{"name": names.treasury, "addressing_policy": open_}], names).status == dr.FAIL
    assert dr.treasury_owner_only([], names).status == dr.FAIL
