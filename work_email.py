"""Parse supervision work that students email to jbr46@cam.ac.uk and that is
forwarded to Gmail. Standard library only: shared by the server's
sync_work_email.py and the Mac's mac/marking_sync.py."""

from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
import html
import re
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
FORWARDER = "jbr46@cam.ac.uk"
GMAIL_QUERY = f"from:{FORWARDER} has:attachment newer_than:14d"
# Ignore small images (email signatures, logos) when deciding if work is attached.
MIN_IMAGE_BYTES = 30_000
WORK_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".doc", ".docx", ".note", ".goodnotes", ".zip")

_FORWARDED_FROM = re.compile(r"From:\s*[^\n<]*<?\s*([a-z]+[0-9]+)@cam\.ac\.uk", re.IGNORECASE)
CRSID = re.compile(r"[a-z]+[0-9]+")


def group_crsids(name):
    """CRSids in a booking name, which students type as e.g. 'abc12, def34' or 'abc12 def34'."""
    return [crsid for crsid in re.split(r"[\s,]+", name or "") if crsid]


def _body_text(message):
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text


def work_attachments(message):
    """(filename, bytes) for each attachment that looks like supervision work."""
    found = []
    for part in message.walk():
        filename = part.get_filename() or ""
        if not filename or part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_content_maintype() == "image" and len(payload) < MIN_IMAGE_BYTES:
            continue
        if filename.lower().endswith(WORK_EXTENSIONS) or part.get_content_type() == "application/pdf":
            found.append((filename, payload))
    return found


_FORWARDED_NAME = re.compile(r"From:\s*\"?([^\n<\"]+?)\"?\s*<\s*[a-z]+[0-9]+@cam\.ac\.uk", re.IGNORECASE)


def forwarded_sender_name(raw):
    """The display name in the forwarded "From:" line, e.g. 'Fedor Kuznetsov', or None."""
    match = _FORWARDED_NAME.search(_body_text(message_from_bytes(raw, policy=policy.default)))
    name = match.group(1).strip() if match else ""
    return name if name and "@" not in name and len(name) <= 100 else None


def message_id_of(header_bytes):
    message = message_from_bytes(header_bytes, policy=policy.default)
    return (message.get("Message-ID") or "").strip()


def parse_forward(raw):
    """Return (message_id, crsid, received_at, attachments) for a forwarded work
    email, or None. received_at is naive Europe/London time."""
    message = message_from_bytes(raw, policy=policy.default)
    message_id = (message.get("Message-ID") or "").strip()
    attachments = work_attachments(message)
    if not message_id or not attachments:
        return None
    match = _FORWARDED_FROM.search(_body_text(message))
    if not match or match.group(1).lower() == FORWARDER.split("@")[0]:
        return None
    sent = parsedate_to_datetime(message["Date"])
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=LONDON)
    received_at = sent.astimezone(LONDON).replace(tzinfo=None, microsecond=0)
    return message_id, match.group(1).lower(), received_at, attachments
