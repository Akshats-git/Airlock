"""Gate 0 spike, checks 6-8: a bare protocol agent against the local bridge.

Subcommands (run with: uv run --with websockets python spike/protocol_spike.py ...):
  register            register spike-proto with $SWITCH_REG_KEY, save its key to runtime/
  ops                 list the operations and their parameters (learn signatures, don't guess)
  call OP [JSON]      call one operation with the saved key (WITH_CONN=1 adds the connection header)
  say ROOM TEXT...    post via POST /agents/{id}/message (needs no connection)
  listen ROOM [SECS]  open the socket, answer pings, log events, default 130 s

Env: SWITCH_API (default http://localhost:43381), SWITCH_REG_KEY (register only).
"""
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = os.environ.get("SWITCH_API", "http://localhost:43381").rstrip("/")
NAME = "spike-proto"
STATE = Path(__file__).resolve().parent.parent / "runtime" / "spike-proto.json"


def http(method, path, body=None, key=None, conn=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if conn:
        headers["X-Switch-Connection-Id"] = conn
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def load():
    if not STATE.exists():
        sys.exit(f"not registered yet: run `register` first ({STATE} missing)")
    return json.loads(STATE.read_text())


def register():
    reg_key = os.environ.get("SWITCH_REG_KEY")
    if not reg_key:
        sys.exit("set SWITCH_REG_KEY to the registration key from the Gateway's API Keys page")
    if STATE.exists():
        sys.exit(f"already registered ({STATE}); delete it to start over. Re-registering "
                 "without overwrite is a 409, with overwrite it rotates the key.")
    body = {
        "name": NAME,
        "description": "Airlock Gate 0 throwaway protocol agent",
        "connector_type": "airlock-spike",
        "integration_profile": {
            "connection_model": "always_on",
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
    }
    status, res = http("POST", "/agents", body, key=reg_key)
    print(status, {k: ("<hidden>" if k == "api_key" else v) for k, v in res.items()}
          if isinstance(res, dict) else res)
    if status != 200 and status != 201:
        sys.exit(1)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"id": res["id"], "api_key": res["api_key"],
                                 "connection_id": str(uuid.uuid4())}))
    STATE.chmod(0o600)
    print(f"saved to {STATE}")


def ops():
    s = load()
    status, res = http("GET", f"/agents/{s['id']}/ops", key=s["api_key"])
    if status != 200:
        sys.exit(f"{status} {res}")
    for name, spec in sorted(res["operations"].items()):
        props = spec.get("input_schema", {}).get("properties", {})
        req = set(spec.get("input_schema", {}).get("required", []))
        sig = ", ".join(f"{p}{'' if p in req else '?'}:{v.get('type', '?')}" for p, v in props.items())
        print(f"{name}({sig})\n    {spec.get('description', '').strip().splitlines()[0] if spec.get('description') else ''}")


def call(op, args):
    s = load()
    # The connection header binds a call to a room and needs an open socket; send it only on request.
    conn = s["connection_id"] if os.environ.get("WITH_CONN") else None
    status, res = http("POST", f"/agents/{s['id']}/ops/{op}", args, key=s["api_key"], conn=conn)
    print(status, json.dumps(res, indent=2)[:4000])


def say(room, text):
    s = load()
    status, res = http("POST", f"/agents/{s['id']}/message", {"room_id": room, "content": text},
                       key=s["api_key"])
    print(status, res)


async def listen(room, seconds):
    import websockets  # provided by `uv run --with websockets`

    s = load()
    ws_base = API.replace("http://", "ws://").replace("https://", "wss://")
    url = (f"{ws_base}/agents/{s['id']}/connection/ws?connection_id={s['connection_id']}"
           f"&rooms={room}&client=airlock-spike&client_version=0")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    pings = pongs = addressed = 0
    cursor = 0
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {s['api_key']}"}) as ws:
        while loop.time() - t0 < seconds:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=seconds - (loop.time() - t0))
            except asyncio.TimeoutError:
                break
            f = json.loads(raw)
            kind = f.get("type") or f.get("event")
            if kind == "ping":
                pings += 1
                await ws.send(json.dumps({"type": "pong", "cursor": cursor}))
                pongs += 1
                continue
            if f.get("event") in ("message", "command", "room_join"):
                cursor = f.get("id", cursor)
                p = f["data"]["payload"]
                if f["event"] == "message" and p.get("addressed"):
                    addressed += 1
                print(f"[{loop.time() - t0:6.1f}s] {f['event']} seq={cursor} "
                      f"addressed={p.get('addressed')} from={p.get('sender_name')}: {str(p.get('body'))[:120]!r}")
            else:
                print(f"[{loop.time() - t0:6.1f}s] CONTROL {json.dumps(f)[:300]}")
    print(f"done: held {loop.time() - t0:.0f}s, pings={pings}, pongs={pongs}, addressed messages={addressed}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "register":
        register()
    elif cmd == "ops":
        ops()
    elif cmd == "call" and len(argv) >= 3:
        call(argv[2], json.loads(argv[3]) if len(argv) > 3 else {})
    elif cmd == "say" and len(argv) >= 4:
        say(argv[2], " ".join(argv[3:]))
    elif cmd == "listen" and len(argv) >= 3:
        asyncio.run(listen(argv[2], float(argv[3]) if len(argv) > 3 else 130))
    else:
        print(__doc__)


main(sys.argv)
