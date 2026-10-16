"""One-off setup for mac/marking_sync.py. Run it yourself in Terminal:

    python3 mac/setup_marking.py

It asks for the Gmail app password (stored in the macOS Keychain, never on
disk), copies the website's admin API token from the server into the
Keychain, installs a launchd agent that runs marking_sync.py every 10
minutes, and does a first run so macOS can ask for permission to use Mail
and iCloud Drive.
"""

from getpass import getpass
from pathlib import Path
import imaplib
import json
import os
import plistlib
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import marking_sync as sync  # noqa: E402

LABEL = "com.benji.supervision-marking"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
PYTHON = HERE.parent / ".venv/bin/python"


def store_secret(service, secret):
    subprocess.run(
        ["security", "add-generic-password", "-U", "-a", os.environ["USER"], "-s", service, "-w", secret],
        check=True, capture_output=True,
    )


def main():
    config = sync.load_json(sync.CONFIG_PATH, {})
    address = input(f"Gmail address [{config.get('gmail_address', '')}]: ").strip() or config.get("gmail_address", "")
    password = getpass("Gmail app password (Enter to keep the saved one): ").replace(" ", "").strip()
    password = password or sync.keychain(sync.KEYCHAIN_GMAIL) or ""
    if not address or not password:
        raise SystemExit("A Gmail address and app password are needed.")
    print("Checking Gmail…")
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com", ssl_context=sync.ssl_context()) as imap:
            imap.login(address, password)
    except imaplib.IMAP4.error as exc:
        raise SystemExit(f"Gmail rejected the sign-in ({exc}).") from None
    store_secret(sync.KEYCHAIN_GMAIL, password)
    sync.SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    sync.CONFIG_PATH.write_text(json.dumps({"gmail_address": address}, indent=1))

    print("Copying the website's admin token from the server…")
    result = subprocess.run(
        ["ssh", "shell_srcf", "grep '^ADMIN_API_TOKEN=' ~/scheduler/.env | cut -d= -f2-"],
        capture_output=True, text=True, timeout=120,
    )
    token = result.stdout.strip()
    if token:
        store_secret(sync.KEYCHAIN_TOKEN, token)
    else:
        print("  Couldn't fetch it; marked work will still be emailed, but not ticked on the website.")

    PLIST.parent.mkdir(parents=True, exist_ok=True)
    log_path = sync.SUPPORT_DIR / "marking.log"
    with PLIST.open("wb") as handle:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": [str(PYTHON), str(HERE / "marking_sync.py")],
            "StartInterval": 600,
            "RunAtLoad": True,
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
        }, handle)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)

    print("First run (macOS may ask to allow access to iCloud Drive and Mail; please allow)…")
    subprocess.run([str(PYTHON), str(HERE / "marking_sync.py")], check=False)
    print(f"\nDone. Work will appear in iCloud Drive › {sync.MARKING_ROOT.name}.")
    print(f"Log: {log_path}")


if __name__ == "__main__":
    main()
