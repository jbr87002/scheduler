import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mac"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import marking_sync as sync
from test_sync_work_email import forward


SLOTS = [
    {"id": 1, "start_time": "2026-10-10T12:00:00", "end_time": "2026-10-10T13:00:00",
     "is_available": False, "name": "sk2476 ir387", "location": "Clare"},
    {"id": 2, "start_time": "2026-10-17T12:00:00", "end_time": "2026-10-17T13:00:00",
     "is_available": False, "name": "sk2476 ir387 fk420", "location": "Clare"},
    {"id": 3, "start_time": "2026-10-17T09:00:00", "end_time": "2026-10-17T10:00:00",
     "is_available": True, "name": None, "location": "CMI"},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "MARKING_ROOT", tmp_path / "Supervision marking")
    monkeypatch.setattr(sync, "SUPPORT_DIR", tmp_path / "support")
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "support/state.json")
    monkeypatch.setattr(sync, "DRY_RUN", False)
    monkeypatch.setattr(sync, "request_downloads", lambda: None)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(SLOTS).encode()

    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda *a, **k: Response())
    sync.MARKING_ROOT.mkdir(parents=True)
    sent = []
    monkeypatch.setattr(sync, "send_email", lambda to, subject, body, path: sent.append((to, subject, body, path.name)))
    ticked = []
    monkeypatch.setattr(sync, "mark_on_website", lambda token, slot_id, crsid: ticked.append((slot_id, crsid)))
    return sync.new_state(), sync.booked_slots(), sent, ticked


def test_emailed_work_is_filed_under_its_next_supervision(env):
    state, booked, _, _ = env
    parsed = sync.parse_forward(forward("Fedor Kuznetsov <fk420@cam.ac.uk>"))
    sync.save_work("<m1>", parsed, state, booked)
    saved = list(sync.MARKING_ROOT.rglob("*.pdf"))
    assert [p.relative_to(sync.MARKING_ROOT).as_posix() for p in saved] == [
        "Week of 2026-10-12/Sat 17 Oct 1200 – sk2476 ir387 fk420/fk420 – work.pdf"
    ]
    assert state["folders"]["Week of 2026-10-12/Sat 17 Oct 1200 – sk2476 ir387 fk420"]["slot_id"] == 2


def test_marked_file_is_sent_once_to_its_student_and_ticked(env):
    state, booked, sent, ticked = env
    sync.save_work("<m1>", sync.parse_forward(forward("Fedor Kuznetsov <fk420@cam.ac.uk>")), state, booked)
    original = next(sync.MARKING_ROOT.rglob("*.pdf"))
    marked = original.with_name("fk420 – work_marked.pdf")
    original.rename(marked)
    old = time.time() - 600
    os.utime(marked, (old, old))

    sync.send_marked(state, booked, token="t")
    sync.send_marked(state, booked, token="t")

    assert sent == [("fk420@cam.ac.uk", "", "", "fk420 – work_marked.pdf")]
    assert ticked == [(2, "fk420")]


def test_waits_for_icloud_to_settle_and_ignores_unmarked_files(env):
    state, booked, sent, _ = env
    folder = sync.MARKING_ROOT / "Unmatched"
    folder.mkdir()
    (folder / "fk420 – fresh_marked.pdf").write_bytes(b"x")
    (folder / "fk420 – not yet.pdf").write_bytes(b"x")
    sync.send_marked(state, booked, token=None)
    assert sent == []


def test_never_sends_to_unknown_or_missing_crsids(env):
    state, booked, sent, _ = env
    folder = sync.MARKING_ROOT / "Unmatched"
    folder.mkdir()
    old = time.time() - 600
    for name in ("zz999 – work_marked.pdf", "Scan_marked.pdf", "fk4200 – work_marked.pdf"):
        (folder / name).write_bytes(b"x")
        os.utime(folder / name, (old, old))
    sync.send_marked(state, booked, token="t")
    assert sent == []
    assert len(state["rejected"]) == 3


def test_work_with_no_upcoming_supervision_goes_to_unmatched(env):
    state, booked, _, _ = env
    parsed = sync.parse_forward(forward("Fedor Kuznetsov <fk420@cam.ac.uk>", date="Mon, 30 Nov 2026 10:00:00 +0000"))
    sync.save_work("<m2>", parsed, state, booked)
    assert [p.parent.name for p in sync.MARKING_ROOT.rglob("*.pdf")] == ["Unmatched"]
