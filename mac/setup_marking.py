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
import shlex
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import marking_sync as sync  # noqa: E402

LABEL = "com.benji.supervision-marking"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
APP = Path.home() / "Applications/Supervision Marking.app"
# The project's venv on the laptop; the Python running this script elsewhere
# (e.g. /usr/bin/python3 on the Mac Studio). marking_sync.py needs only the stdlib.
PYTHON = HERE.parent / ".venv/bin/python"
if not PYTHON.exists():
    # /usr/bin/python3 is a stable shim; sys.executable may be inside Xcode.
    PYTHON = Path("/usr/bin/python3") if Path("/usr/bin/python3").exists() else Path(sys.executable)


def store_secret(service, secret):
    """Save a secret in the login keychain without it ever appearing in a
    command line, process list or error message: `security -i` reads the
    command from stdin."""
    if any(c in secret for c in '"\\\n'):
        raise SystemExit("That secret contains characters this script can't store safely.")
    command = f'add-generic-password -U -a "{os.environ["USER"]}" -s "{service}" -w "{secret}"\n'
    result = subprocess.run(["security", "-i"], input=command, text=True, capture_output=True)
    stored = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                            capture_output=True, text=True)
    if result.returncode != 0 or stored.stdout.strip() != secret:
        raise SystemExit(
            "Couldn't save to the keychain. If you're connected over SSH, the login keychain is "
            "locked: run `security unlock-keychain ~/Library/Keychains/login.keychain-db` first, "
            "or run this in Terminal on the Mac itself."
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

    install_agent()
    print(f"\nDone. Work will appear in iCloud Drive › {sync.MARKING_ROOT.name}.")


def build_app(log_path):
    """A tiny background app that runs marking_sync.py. It gives the job its own
    identity for macOS privacy permissions (Full Disk Access for iCloud Drive,
    controlling Mail), rather than granting them to every Python script."""
    command = (f"{shlex.quote(str(PYTHON))} {shlex.quote(str(HERE / 'marking_sync.py'))}"
               f" >> {shlex.quote(str(log_path))} 2>&1")
    APP.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rm", "-rf", str(APP)], check=True)
    subprocess.run(["osacompile", "-o", str(APP),
                    "-e", "try", "-e", f"do shell script {json.dumps(command)}", "-e", "end try"], check=True)
    info_path = APP / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update(LSUIElement=True, CFBundleIdentifier=LABEL, CFBundleName="Supervision Marking")
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--sign", "-", str(APP)], check=True, capture_output=True)


def install_agent():
    sync.SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = sync.SUPPORT_DIR / "marking.log"
    build_app(log_path)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as handle:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": [str(APP / "Contents/MacOS/applet")],
            "StartInterval": 600,
            "RunAtLoad": True,
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
        }, handle)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    print(f"""
Installed {APP.name}, which runs every 10 minutes. Two macOS permissions it needs:
  1. System Settings › Privacy & Security › Full Disk Access: click +, press ⌘⇧G,
     go to {APP.parent} and add "{APP.name}" (switched on).
  2. When it first sends marked work, allow "Supervision Marking" to control Mail.
Log: {log_path}""")


if __name__ == "__main__":
    if sys.argv[1:] == ["--reinstall-agent"]:
        install_agent()  # Rebuild the app and launch agent without re-entering secrets.
    else:
        main()
