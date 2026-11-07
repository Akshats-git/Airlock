"""The mailroom stays on: whatever lands in inbox/new goes to quarantine as one run, then into cur/."""
import os
import shutil
from pathlib import Path

from airlock.agents import mailroom, wire
from airlock.agents.common import Names

FIX = Path(__file__).resolve().parent.parent / "fixtures"


class Poster:
    def __init__(self):
        self.posts = []

    def post(self, room, text):
        self.posts.append((room, text))


def test_an_empty_inbox_posts_nothing(tmp_path):
    assert mailroom.watch_once(Poster(), "q", tmp_path, Names(), delay=0) is None


def test_new_mail_goes_as_one_run_and_is_filed(tmp_path):
    new = tmp_path / "new"
    new.mkdir()
    for i, f in enumerate(sorted((FIX / "emails").glob("*.eml"))):
        # names a mail fetcher would give them
        dest = new / f"1762500{i}.M{i}P42.mailhost:2,"
        shutil.copy(f, dest)
        os.utime(dest, (1000 + i, 1000 + i))
    p = Poster()
    run = mailroom.watch_once(p, "q", tmp_path, Names(), delay=0)
    assert run and len(p.posts) == 8  # an ENVELOPE and a MAIL for each of four
    envs = [wire.parse_envelope(t) for _, t in p.posts[::2]]
    assert [e.domain for e in envs] == ["acme.example", "globex.example", "northwind.example", "initech-billing.example"]
    assert all(e.total == 4 and e.run == run for e in envs)
    assert all(wire.parse_mail(t) is not None for _, t in p.posts[1::2])
    assert not any(new.iterdir()) and len(list((tmp_path / "cur").iterdir())) == 4


def test_odd_file_names_still_make_valid_unique_mail_ids(tmp_path):
    new = tmp_path / "new"
    new.mkdir()
    for name in ("___.eml", "Invoice #1.eml", "Invoice #1 .eml", "x" * 80):
        (new / name).write_text("From: a@acme.example\n\nhi\n")
    ids = [m.mail_id for _, m in mailroom.collect(tmp_path)]
    assert len(set(ids)) == 4 and all(wire.valid_mail_id(i) for i in ids)
