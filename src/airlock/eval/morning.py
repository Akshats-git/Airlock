"""One normal morning (fixtures/morning) through Airlock's own code, end to end, with no chat server.

The same path as the live desk: the mailroom's messages for all 30 emails as one run, the reader (recorded
records by default, or the real model), customs' one card, the clerk's single `ap match` over the whole card,
the specialist's decisions on the exceptions, the batch, and treasury's payment run plus one `bank pay` per
payment that needs its own card. It reports what people had to do, and scores every payment with the same
judge as the attack corpus.
"""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from pathlib import Path

from airlock import postman
from airlock.agents import mailroom, reader, wire
from airlock.agents.common import Names
from airlock.agents.customs import Intake
from airlock.bank import ops as bank_ops
from airlock.customs import gate
from airlock.erp import db as erp_db
from airlock.erp import ops as erp_ops
from airlock.erp.seed import seed
from airlock.eval.airlock_run import DAY, RUN, _treasury
from airlock.eval.truth import Ledger
from airlock.schema import ClearedRecord, format_money

MORNING = Path("fixtures/morning")


def run(directory: Path = MORNING, extractor=None) -> dict:
    names = Names()
    mails = mailroom.load_mail(directory)
    msgs = mailroom.plan(mails, names, RUN)
    extractor = extractor or reader.FixtureExtractor(Path("fixtures/reader"))
    with tempfile.TemporaryDirectory(prefix="airlock-morning-") as tmp:
        erp = erp_db.connect(Path(tmp) / "erp.sqlite")
        seed(erp)
        bank = bank_ops.connect(Path(tmp) / "bank.sqlite")
        vendors = gate.load_vendors(erp)
        intake = Intake(lambda: vendors, names, day=DAY)
        card = None
        for env_msg, mail_msg in zip(msgs[::2], msgs[1::2]):
            intake.on_envelope(wire.parse_envelope(env_msg))
            res = wire.parse_read(reader.handle(wire.parse_mail(mail_msg), extractor, names))
            card = intake.on_read(res) or card
        lines = card.text.splitlines()
        held = [ln for ln in lines if ln.startswith("HELD ")]
        recs = [ClearedRecord.model_validate_json(ln[4:]) for ln in lines if ln.startswith("REC ")]
        results = [erp_ops.match(erp, r) for r in recs]  # what one `./bin/ap match ...` does
        exceptions = [r for r in results if r.status == "exception"]
        decisions = [erp_ops.resolve(erp, r.invoice_id, "short-pay", "sam") for r in exceptions]
        _batch, doc = erp_ops.propose_batch(erp)
        paid_lines = _treasury(doc, bank, erp)
        run_line = re.search(r"^RUN (.+)$", doc, re.M)
        own = re.findall(r"^PAY-\d{4} .+ \| (over .+|account changed .+)$", doc, re.M)
        ledger, harms = Ledger([]), []
        for t in bank.execute("SELECT * FROM ledger").fetchall():
            p = erp.execute("SELECT p.vendor_id, i.po_id FROM payments p JOIN invoices i ON i.id=p.invoice_id"
                            " WHERE p.id=?", (t["payment_id"],)).fetchone()
            harms += ledger.judge_payment(p["vendor_id"], t["iban"], t["amount_cents"], t["currency"], p["po_id"])
        paid = bank.execute("SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM ledger").fetchone()
        holds = [{"record_id": h.record_id, "vendor_id": h.vendor_id, "reasons": list(h.reasons)} for h in card.holds]
        letters = Counter(x.kind for x in postman.due(bank, erp, holds))
        out = dict(
            emails=len(mails), cleared=card.cleared, held=len(held),
            held_reasons=[h.split("reasons=")[1] for h in held],
            matched=sum(r.status == "matched" for r in results), exceptions=len(exceptions),
            duplicates=sum(r.status == "duplicate" for r in results),
            payments=paid[0], paid_cents=paid[1], run_payments=int(re.search(r"--count (\d+)", run_line[1])[1]) if run_line else 0,
            own_cards=len(own), approvals=(1 if run_line else 0) + len(own), human_decisions=len(decisions),
            refused=[ln for ln in paid_lines if "REFUSED" in ln], harms=sorted(set(harms)), card=card.text, batch=doc,
            letters=dict(letters))
        erp.close()
        bank.close()
    return out


def report(m: dict) -> str:
    reasons = Counter(r for h in m["held_reasons"] for r in h.split(","))
    return "\n".join([
        f"{m['emails']} emails in one morning",
        f"customs: {m['cleared']} cleared, {m['held']} held ({', '.join(f'{k} {v}' for k, v in reasons.most_common())})",
        f"clerk, one command: {m['matched']} matched, {m['exceptions']} exceptions, {m['duplicates']} duplicate",
        f"the AP specialist: {m['human_decisions']} decisions (short-pay)",
        f"treasury: {m['payments']} payments, USD {format_money(m['paid_cents'])}",
        f"the controller: {m['approvals']} approvals ({m['run_payments']} routine payments on one card, "
        f"{m['own_cards']} large payments on their own)",
        f"harm: {', '.join(m['harms']) or 'none'}; refused by the bank: {len(m['refused'])}",
        "letters to vendors, by plain code: " + ", ".join(f"{v} {LETTERS[k]}" for k, v in m.get("letters", {}).items()),
    ])


LETTERS = {"remittance": "remittance advice", "credit-note": "credit note request",
           "bank-change-request": "bank-detail request notice", "bank-change-applied": "bank-detail change notice"}
