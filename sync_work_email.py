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
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
import html
import imaplib
import os
import re
import sys

FORWARDER = "jbr46@cam.ac.uk"
LOOKBACK_DAYS = 14
# Ignore small images (email signatures, logos) when deciding if work is attached.
MIN_IMAGE_BYTES = 30_000
WORK_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".doc", ".docx", ".note", ".goodnotes", ".zip")
# Latest a supervision can be after the email and still be the one the work is for.
MATCH_WINDOW = timedelta(days=10)

_FORWARDED_FROM = re.compile(r"From:\s*[^\n<]*<?\s*([a-z]+[0-9]+)@cam\.ac\.uk", re.IGNORECASE)


def configured():
    return bool(os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"))


def _body_text(message):
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text


def _has_work_attachment(message):
    for part in message.walk():
        filename = (part.get_filename() or "").lower()
        if not filename or part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_content_maintype() == "image" and len(payload) < MIN_IMAGE_BYTES:
            continue
        if filename.endswith(WORK_EXTENSIONS) or part.get_content_type() == "application/pdf":
            return True
    return False


def parse_forward(raw):
    """Return (message_id, crsid, received_at) for a forwarded work email, or None.

    received_at is naive Europe/London time, like the rest of the database.
    """
    from app import london_tz

    message = message_from_bytes(raw, policy=policy.default)
    message_id = (message.get("Message-ID") or "").strip()
    if not message_id or not _has_work_attachment(message):
        return None
    match = _FORWARDED_FROM.search(_body_text(message))
    if not match or match.group(1).lower() == FORWARDER.split("@")[0]:
        return None
    sent = parsedate_to_datetime(message["Date"])
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=london_tz)
    received_at = sent.astimezone(london_tz).replace(tzinfo=None, microsecond=0)
    return message_id, match.group(1).lower(), received_at


def _log_processed(message_id, outcome):
    from app import db, ProcessedEmail, london_tz

    db.session.add(ProcessedEmail(
        message_id=message_id[:255],
        processed_at=datetime.now(london_tz).replace(tzinfo=None, microsecond=0),
        outcome=outcome[:200],
    ))
    db.session.commit()


def record_work(message_id, crsid, received_at):
    """Tick the student's next supervision as received online. Returns a short outcome."""
    from app import db, TimeSlot, WorkSubmission, ProcessedEmail, _group_crsids

    if db.session.get(ProcessedEmail, message_id):
        return None
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


def _message_id_of(header_bytes):
    message = message_from_bytes(header_bytes, policy=policy.default)
    return (message.get("Message-ID") or "").strip()


def process_mailbox(imap):
    """Handle each recent forward once; already-processed emails are skipped
    after reading only their Message-ID header."""
    from app import db, ProcessedEmail

    imap.select('"[Gmail]/All Mail"', readonly=True)
    query = f"from:{FORWARDER} has:attachment newer_than:{LOOKBACK_DAYS}d"
    status, data = imap.search(None, "X-GM-RAW", f'"{query}"')
    if status != "OK":
        raise RuntimeError(f"Gmail search failed: {status}")
    outcomes = []
    for number in data[0].split():
        status, parts = imap.fetch(number, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if status != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        message_id = _message_id_of(parts[0][1])
        if not message_id or db.session.get(ProcessedEmail, message_id):
            continue
        status, parts = imap.fetch(number, "(BODY.PEEK[])")
        if status != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        parsed = parse_forward(parts[0][1])
        if parsed:
            outcomes.append(record_work(*parsed))
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
