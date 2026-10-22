"""Mark supervision work as received when a student emails it.

Students email work to jbr46@cam.ac.uk, which forwards it to Gmail. This
script (run every few minutes by the scheduler-mail-sync systemd timer) reads
recent forwards with attachments from Gmail over IMAP, finds the original
sender's CRSid in the forwarded header, and ticks the student's next
supervision as received online.

Configure with configure_gmail.py. Without GMAIL_ADDRESS/GMAIL_APP_PASSWORD
it does nothing.
"""

from datetime import datetime, timedelta
import imaplib
import os
import sys

from work_email import GMAIL_QUERY, forwarded_sender_name, message_id_of, parse_forward as _parse

# Latest a supervision can be after the email and still be the one the work is for.
MATCH_WINDOW = timedelta(days=10)


def configured():
    return bool(os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"))


def parse_forward(raw):
    """Return (message_id, crsid, received_at) for a forwarded work email, or None."""
    parsed = _parse(raw)
    return parsed[:3] if parsed else None


def _log_processed(message_id, outcome):
    from app import db, ProcessedEmail, london_tz

    db.session.add(ProcessedEmail(
        message_id=message_id[:255],
        processed_at=datetime.now(london_tz).replace(tzinfo=None, microsecond=0),
        outcome=outcome[:200],
    ))
    db.session.commit()


def record_work(message_id, crsid, received_at, name=None):
    """Tick the student's next supervision as received online. Returns a short outcome.

    name (from the email's From line) is saved for the CRSid if none is known yet.
    """
    from app import db, TimeSlot, WorkSubmission, ProcessedEmail, Student, _group_crsids

    if db.session.get(ProcessedEmail, message_id):
        return None
    if name and not db.session.get(Student, crsid):
        db.session.add(Student(crsid=crsid, name=name))
    candidates = (
        TimeSlot.query.filter(
            TimeSlot.is_available == False,  # noqa: E712
            TimeSlot.end_time > received_at,
            TimeSlot.start_time <= received_at + MATCH_WINDOW,
            TimeSlot.name.contains(crsid),
        )
        .order_by(TimeSlot.start_time)
        .all()
    )
    slot = next((s for s in candidates if crsid in _group_crsids(s.name)), None)
    if not slot:
        outcome = f"no upcoming supervision for {crsid}"
    elif WorkSubmission.query.filter_by(slot_id=slot.id, crsid=crsid).first():
        outcome = f"{crsid} already recorded for slot {slot.id}"
    else:
        db.session.add(WorkSubmission(
            slot_id=slot.id, crsid=crsid, format="online", marked=False,
            source="email", received_at=received_at,
        ))
        outcome = f"recorded {crsid} for slot {slot.id} ({slot.start_time:%a %d %b %H:%M})"
    _log_processed(message_id, outcome)
    return outcome


def process_mailbox(imap):
    """Handle each recent forward once; already-processed emails are skipped
    after reading only their Message-ID header."""
    from app import db, ProcessedEmail

    imap.select('"[Gmail]/All Mail"', readonly=True)
    status, data = imap.search(None, "X-GM-RAW", f'"{GMAIL_QUERY}"')
    if status != "OK":
        raise RuntimeError(f"Gmail search failed: {status}")
    outcomes = []
    for number in data[0].split():
        status, parts = imap.fetch(number, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if status != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        message_id = message_id_of(parts[0][1])
        if not message_id or db.session.get(ProcessedEmail, message_id):
            continue
        status, parts = imap.fetch(number, "(BODY.PEEK[])")
        if status != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        raw = parts[0][1]
        parsed = parse_forward(raw)
        if parsed:
            outcomes.append(record_work(*parsed, name=forwarded_sender_name(raw)))
        else:
            _log_processed(message_id, "not student work")
    return [o for o in outcomes if o]


def main():
    from app import app

    if not configured():
        print("Gmail is not configured; nothing to do.")
        return 0
    with app.app_context():
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
            for outcome in process_mailbox(imap):
                app.logger.info(f"Work email: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
