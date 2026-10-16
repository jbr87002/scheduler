"""Mac side of supervision marking. Runs every 10 minutes (launchd agent
com.benji.supervision-marking, installed by setup_marking.py).

1. Work emailed by students (forwarded to Gmail) is saved to
   iCloud Drive/Supervision marking/<week>/<session>/<crsid> – <file>,
   so it appears on the iPad in Files for marking.
2. A file renamed to end in "_marked" (e.g. "fk420 – work_marked.pdf") is
   emailed back to that student from jbr46@cam.ac.uk via Mail, and ticked as
   marked on jbr46.com.

Safety: a file is only sent to the CRSid that starts its name, only if that
CRSid is in a booking, and only once (recorded in the state file before the
next file is considered). Set MARKING_DRY_RUN=1 to log instead of sending.
"""

from datetime import datetime, timedelta
from pathlib import Path
import imaplib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from work_email import CRSID, GMAIL_QUERY, group_crsids, message_id_of, parse_forward  # noqa: E402

SITE = "https://jbr46.com"
MARKING_ROOT = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Supervision marking"
SUPPORT_DIR = Path.home() / "Library/Application Support/SupervisionMarking"
STATE_PATH = SUPPORT_DIR / "state.json"
CONFIG_PATH = SUPPORT_DIR / "config.json"
KEYCHAIN_GMAIL = "supervision-marking-gmail"
KEYCHAIN_TOKEN = "supervision-marking-admin-token"
MATCH_WINDOW = timedelta(days=10)
# Wait this long after the last change before sending, so iCloud has finished
# syncing the annotations from the iPad.
SETTLE_SECONDS = 120
MARKED_SUFFIX = "_marked"
DRY_RUN = os.getenv("MARKING_DRY_RUN") == "1"


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


# ---- Config, secrets and state ----------------------------------------------

def keychain(service):
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state):
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE_PATH)


def new_state():
    return {"messages": {}, "folders": {}, "sent": {}, "rejected": {}}


# ---- Bookings (public timetable) --------------------------------------------

def booked_slots():
    with urllib.request.urlopen(f"{SITE}/api/get_timeslots", timeout=60) as response:
        slots = json.load(response)
    booked = [s for s in slots if not s["is_available"]]
    for slot in booked:
        slot["start"] = datetime.fromisoformat(slot["start_time"])
        slot["end"] = datetime.fromisoformat(slot["end_time"])
        slot["crsids"] = [c.lower() for c in group_crsids(slot["name"])]
    return booked


def slot_for(crsid, received_at, booked):
    candidates = [
        s for s in booked
        if crsid in s["crsids"] and s["end"] > received_at and s["start"] <= received_at + MATCH_WINDOW
    ]
    return min(candidates, key=lambda s: s["start"]) if candidates else None


def session_folder(slot):
    monday = slot["start"].date() - timedelta(days=slot["start"].weekday())
    week = f"Week of {monday:%Y-%m-%d}"
    session = f"{slot['start']:%a %d %b %H%M} – {' '.join(slot['crsids'])}"
    return Path(week) / session


# ---- 1. Gmail → iCloud Drive -------------------------------------------------

def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]', "-", name).strip() or "work"


def unique_path(path):
    if not path.exists():
        return path
    for n in range(2, 100):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many files named like {path}")


def fetch_new_work(gmail_address, gmail_password, state, booked):
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(gmail_address, gmail_password)
        imap.select('"[Gmail]/All Mail"', readonly=True)
        status, data = imap.search(None, "X-GM-RAW", f'"{GMAIL_QUERY}"')
        if status != "OK":
            raise RuntimeError(f"Gmail search failed: {status}")
        for number in data[0].split():
            status, parts = imap.fetch(number, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            message_id = message_id_of(parts[0][1])
            if not message_id or message_id in state["messages"]:
                continue
            status, parts = imap.fetch(number, "(BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            save_work(message_id, parse_forward(parts[0][1]), state, booked)
            save_state(state)


def save_work(message_id, parsed, state, booked):
    if not parsed:
        state["messages"][message_id] = "not student work"
        return
    _, crsid, received_at, attachments = parsed
    slot = slot_for(crsid, received_at, booked)
    folder = session_folder(slot) if slot else Path("Unmatched")
    (MARKING_ROOT / folder).mkdir(parents=True, exist_ok=True)
    if slot:
        state["folders"][str(folder)] = {"slot_id": slot["id"], "start": slot["start_time"]}
    saved = []
    for filename, payload in attachments:
        path = unique_path(MARKING_ROOT / folder / safe_name(f"{crsid} – {filename}"))
        path.write_bytes(payload)
        saved.append(str(path.relative_to(MARKING_ROOT)))
    state["messages"][message_id] = {"crsid": crsid, "files": saved}
    log(f"Saved work from {crsid}: {', '.join(saved)}")


# ---- 2. "_marked" files → students -------------------------------------------

def marked_files():
    for path in sorted(MARKING_ROOT.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.stem.lower().endswith(MARKED_SUFFIX):
            yield path


def request_downloads():
    """Files iCloud hasn't downloaded to this Mac show up as .name.icloud placeholders."""
    for placeholder in MARKING_ROOT.rglob(".*.icloud"):
        real_name = placeholder.name[1:-len(".icloud")]
        if Path(real_name).stem.lower().endswith(MARKED_SUFFIX):
            subprocess.run(["brctl", "download", str(placeholder.with_name(real_name))], capture_output=True)


SEND_SCRIPT = '''
on run argv
    set {toAddr, subj, bodyText, filePath} to argv
    tell application "Mail"
        set m to make new outgoing message with properties {subject:subj, content:bodyText & return & return, visible:false, sender:"B. Rowlands <jbr46@cam.ac.uk>"}
        tell m to make new to recipient at end of to recipients with properties {address:toAddr}
        tell content of m to make new attachment with properties {file name:(POSIX file filePath) as alias} at after last paragraph
        delay 2
        send m
    end tell
end run
'''


def send_email(to_address, subject, body, path):
    if DRY_RUN:
        log(f"[dry run] would send {path.name} to {to_address}")
        return
    subprocess.run(["osascript", "-", to_address, subject, body, str(path)],
                   input=SEND_SCRIPT, text=True, check=True, capture_output=True)


def mark_on_website(token, slot_id, crsid):
    request = urllib.request.Request(
        f"{SITE}/api/admin/work/{slot_id}/{crsid}", method="PUT",
        data=json.dumps({"marked": True}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        urllib.request.urlopen(request, timeout=60).close()
    except Exception as exc:  # The email has gone; the tick is a convenience.
        log(f"Could not tick {crsid} as marked on the website: {exc}")


def send_marked(state, booked, token):
    known = {c for s in booked for c in s["crsids"]}
    request_downloads()
    for path in marked_files():
        rel = str(path.relative_to(MARKING_ROOT))
        if rel in state["sent"]:
            continue
        if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
            continue
        match = CRSID.match(path.name.lower())
        crsid = match.group(0) if match else None
        if not crsid or crsid not in known:
            if rel not in state["rejected"]:
                state["rejected"][rel] = f"no booked student CRSid at the start of the name ({crsid})"
                log(f"Not sending {rel}: name must start with a booked student's CRSid")
            continue
        folder_info = state["folders"].get(str(Path(rel).parent), {})
        day = (f"{datetime.fromisoformat(folder_info['start']):%A %-d %B}"
               if folder_info.get("start") else None)
        subject = f"Marked supervision work – {day}" if day else "Marked supervision work"
        body = ("Hi,\n\nPlease find attached your marked work"
                + (f" for our supervision on {day}" if day else "")
                + ".\n\nBest wishes,\nBenji")
        send_email(f"{crsid}@cam.ac.uk", subject, body, path)
        if DRY_RUN:
            continue
        state["sent"][rel] = {"to": crsid, "at": datetime.now().isoformat(timespec="seconds")}
        save_state(state)
        log(f"Sent {rel} to {crsid}@cam.ac.uk")
        if token and folder_info.get("slot_id"):
            mark_on_website(token, folder_info["slot_id"], crsid)


def main():
    config = load_json(CONFIG_PATH, {})
    gmail_address = config.get("gmail_address")
    gmail_password = keychain(KEYCHAIN_GMAIL)
    if not gmail_address or not gmail_password:
        log("Not set up yet: run mac/setup_marking.py")
        return 1
    state = load_json(STATE_PATH, new_state())
    for key, value in new_state().items():
        state.setdefault(key, value)
    MARKING_ROOT.mkdir(parents=True, exist_ok=True)
    booked = booked_slots()
    fetch_new_work(gmail_address, gmail_password, state, booked)
    send_marked(state, booked, keychain(KEYCHAIN_TOKEN))
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
