"""Live ledger page: the video's proof that money moved only after Allow once."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from airlock.bank import ops

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Airlock Treasury Ledger</title>
<style>
:root{--paper:#f3eee3;--ink:#1c1b18;--mute:#6f6a5e;--rule:#d8d1c0;--ok:#1d6b49;--tag:#e8e1cf;--font-d:ui-serif,Georgia,"Times New Roman",serif;--font-m:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--paper:#16150f;--ink:#efe9d9;--mute:#a39c88;--rule:#38352a;--ok:#5fd19a;--tag:#25231a}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 var(--font-m)}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}
.eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--mute);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.eyebrow b{background:var(--tag);padding:2px 8px;border-radius:3px;color:var(--ink);font-weight:600}
h1{font:600 clamp(28px,6vw,44px)/1.1 var(--font-d);margin:10px 0 28px;letter-spacing:-.01em}
.bal{border-top:2px solid var(--ink);border-bottom:1px solid var(--rule);padding:18px 0;display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
.bal small{color:var(--mute);font-size:12px;letter-spacing:.1em;text-transform:uppercase}
.bal strong{font:600 clamp(30px,8vw,52px)/1 var(--font-m);font-variant-numeric:tabular-nums}
.out{color:var(--mute);font-size:14px}.out span{color:var(--ok);font-weight:600}
table{width:100%;border-collapse:collapse;margin-top:28px;font-size:14px}
th{text-align:left;font-weight:500;color:var(--mute);font-size:11px;letter-spacing:.12em;text-transform:uppercase;padding:0 8px 8px 0;border-bottom:1px solid var(--ink)}
td{padding:12px 8px 12px 0;border-bottom:1px solid var(--rule);vertical-align:top}
td.n,th.n{text-align:right;padding-right:0;font-variant-numeric:tabular-nums}
tr.new{animation:in .45s cubic-bezier(.2,.7,.2,1)}
@keyframes in{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
.empty{color:var(--mute);padding:28px 0;font-family:var(--font-d);font-style:italic}
.by{color:var(--ok)}
@media (max-width:560px){.hide-s{display:none}}
@media (prefers-reduced-motion:reduce){tr.new{animation:none}}
</style></head><body><main>
<div class="eyebrow"><span>Airlock</span><b>test mode</b><span id="live">connecting</span></div>
<h1>Treasury ledger</h1>
<div class="bal"><div><small>Balance</small><br><strong id="bal">-</strong></div>
<div class="out" id="out"></div></div>
<table><thead><tr><th>Tx</th><th>Payee</th><th class="hide-s">Approved by</th><th class="n">Amount</th></tr></thead>
<tbody id="rows"></tbody></table><div class="empty" id="empty">No transfers yet. Money moves only after the controller allows it.</div>
</main><script>
const fmt=c=>(c/100).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const seen=new Set();let first=true;
async function tick(){try{const r=await fetch("/api/ledger",{cache:"no-store"});const d=await r.json();
document.getElementById("live").textContent="live";
document.getElementById("bal").textContent="$"+fmt(d.balance_cents);
const sent=d.opening_cents-d.balance_cents;
document.getElementById("out").innerHTML=sent?`paid out <span>$${fmt(sent)}</span> in ${d.transfers.length} transfer${d.transfers.length>1?"s":""}`:"";
document.getElementById("empty").style.display=d.transfers.length?"none":"block";
const body=document.getElementById("rows");body.textContent="";
for(const t of d.transfers){const tr=document.createElement("tr");
if(!first&&!seen.has(t.tx_id))tr.className="new";seen.add(t.tx_id);
const cells=[[t.tx_id],[t.payee,t.payment_id+" \u00b7 "+t.iban],[t.approved_by],[t.currency+" "+fmt(t.amount_cents)]];
cells.forEach((c,i)=>{const td=document.createElement("td");
if(i==2)td.className="hide-s by";if(i==3)td.className="n";
td.append(c[0]);if(c[1]){td.append(document.createElement("br"));const s=document.createElement("span");s.className="out";s.textContent=c[1];td.append(s);}
tr.append(td);});body.append(tr);}first=false;}catch(e){document.getElementById("live").textContent="offline"}}
tick();setInterval(tick,1000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/ledger"):
            conn = ops.connect()
            body, ctype = json.dumps(ops.snapshot(conn)).encode(), "application/json"
            conn.close()
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the terminal quiet during recording
        pass


def serve(port: int = 8099) -> None:
    print(f"ledger page on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), _Handler).serve_forever()
