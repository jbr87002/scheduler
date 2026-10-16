import os
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("ADMIN_API_TOKEN", "test-admin-token")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/scheduler-test.sqlite")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import app, db, TimeSlot, WorkSubmission, ProcessedEmail
import sync_work_email as sync


@pytest.fixture(autouse=True)
def clean_database():
    app.config["TESTING"] = True
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def forward(original_from, date="Fri, 16 Oct 2026 13:04:18 +0000", attachment=("work.pdf", "application", "pdf", 200_000),
            message_id="<fw1@cam.ac.uk>", html_body=False):
    msg = EmailMessage()
    msg["From"] = "B. Rowlands <jbr46@cam.ac.uk>"
    msg["To"] = "benji87002@gmail.com"
    msg["Subject"] = "FW: Supervision work"
    msg["Date"] = date
    msg["Message-ID"] = message_id
    header = (f"From: {original_from}\nSent: Friday, 16 October 2026 14:04:15\n"
              "To: B. Rowlands <jbr46@cam.ac.uk>\nSubject: Supervision work\n\nHere is my work.")
    if html_body:
        msg.set_content("<p><b>From:</b> " + original_from.replace("<", "&lt;").replace(">", "&gt;") + "<br>Sent: …</p>",
                        subtype="html")
    else:
        msg.set_content(header)
    if attachment:
        name, maintype, subtype, size = attachment
        msg.add_attachment(b"x" * size, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def booked(name, day, hour=12):
    slot = TimeSlot(start_time=datetime(2026, 10, day, hour), end_time=datetime(2026, 10, day, hour + 1),
                    is_available=False, name=name, location="Clare")
    db.session.add(slot)
    db.session.commit()
    return slot.id


def test_parse_forward_extracts_original_sender_and_london_time():
    message_id, crsid, received_at = sync.parse_forward(forward("Fedor Kuznetsov <fk420@cam.ac.uk>"))
    assert (message_id, crsid) == ("<fw1@cam.ac.uk>", "fk420")
    assert received_at == datetime(2026, 10, 16, 14, 4, 18)  # BST


def test_parse_forward_handles_html_forwards():
    parsed = sync.parse_forward(forward("Jamiul Islam Khan <jik33@cam.ac.uk>", html_body=True))
    assert parsed[1] == "jik33"


def test_parse_forward_ignores_emails_without_real_attachments():
    assert sync.parse_forward(forward("Fedor Kuznetsov <fk420@cam.ac.uk>", attachment=None)) is None
    logo = ("image001.png", "image", "png", 5_000)
    assert sync.parse_forward(forward("Fedor Kuznetsov <fk420@cam.ac.uk>", attachment=logo)) is None


def test_record_work_ticks_next_supervision_once():
    booked("sk2476 ir387", day=10)              # already happened
    target = booked("sk2476 ir387 fk420", day=17)
    booked("sk2476 ir387 fk420", day=24)
    received = datetime(2026, 10, 16, 14, 4)

    assert "recorded fk420" in sync.record_work("<a@x>", "fk420", received)
    assert sync.record_work("<a@x>", "fk420", received) is None  # same email again
    assert "already recorded" in sync.record_work("<b@x>", "fk420", received)

    work = WorkSubmission.query.one()
    assert (work.slot_id, work.crsid, work.format, work.source) == (target, "fk420", "online", "email")


def test_record_work_does_not_match_partial_crsids_or_colleagues():
    booked("sk24760 ir387", day=17)
    assert sync.record_work("<c@x>", "sk2476", datetime(2026, 10, 16, 12)) == "no upcoming supervision for sk2476"
    assert sync.record_work("<d@x>", "pd552", datetime(2026, 10, 16, 12)) == "no upcoming supervision for pd552"
    assert WorkSubmission.query.count() == 0


def test_cleared_tick_is_not_re_added_on_next_run():
    booked("fk420", day=17)
    sync.record_work("<e@x>", "fk420", datetime(2026, 10, 16, 12))
    db.session.delete(WorkSubmission.query.one())
    db.session.commit()
    assert sync.record_work("<e@x>", "fk420", datetime(2026, 10, 16, 12)) is None
    assert WorkSubmission.query.count() == 0


class FakeImap:
    def __init__(self, messages):
        self.messages = messages
        self.full_fetches = 0

    def select(self, mailbox, readonly=False):
        assert readonly
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        assert criteria[0] == "X-GM-RAW"
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, number, what):
        raw = self.messages[int(number) - 1]
        if "HEADER.FIELDS" in what:
            header = b"\r\n".join(line for line in raw.split(b"\n") if line.lower().startswith(b"message-id"))
            return "OK", [(b"", header + b"\r\n\r\n")]
        self.full_fetches += 1
        return "OK", [(b"", raw)]


def test_process_mailbox_downloads_each_email_only_once():
    booked("jik33 jh2696", day=17, hour=13)
    imap = FakeImap([
        forward("Jamiul Islam Khan <jik33@cam.ac.uk>", message_id="<w1@cam.ac.uk>"),
        forward("Pawel Dydio <pd552@cam.ac.uk>", message_id="<w2@cam.ac.uk>", attachment=None),
    ])
    first = sync.process_mailbox(imap)
    assert len(first) == 1 and "recorded jik33" in first[0]
    assert ProcessedEmail.query.count() == 2
    assert imap.full_fetches == 2

    assert sync.process_mailbox(imap) == []
    assert imap.full_fetches == 2
