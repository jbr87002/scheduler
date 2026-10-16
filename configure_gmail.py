"""Run on webserver_srcf to connect Gmail without putting a password in a command or chat.

Needs a Gmail app password (Google Account > Security > 2-Step Verification >
App passwords). sync_work_email.py then uses it to read forwarded work emails.
"""

from getpass import getpass
from pathlib import Path
import imaplib
import os
import re
import tempfile


def main():
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        raise SystemExit("Run this from the deployed scheduler directory with its .env file.")
    current = env_path.read_text()
    existing_email = next(
        (match.group(1) for line in current.splitlines()
         if (match := re.fullmatch(r"GMAIL_ADDRESS=(.*)", line))),
        "",
    )
    email = input(f"Gmail address [{existing_email}]: ").strip() or existing_email
    password = getpass("Gmail app password: ").replace(" ", "").strip()
    if not email or not password or "\n" in email or "\n" in password:
        raise SystemExit("Gmail address and app password are required.")
    print("Checking Gmail access…")
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
            imap.login(email, password)
            status, _ = imap.select('"[Gmail]/All Mail"', readonly=True)
            if status != "OK":
                raise SystemExit("Signed in, but could not open All Mail. Is IMAP enabled in Gmail settings?")
    except imaplib.IMAP4.error as exc:
        raise SystemExit(f"Gmail rejected the sign-in ({exc}). Check the address and app password.") from None

    lines = [line for line in current.splitlines()
             if not line.startswith(("GMAIL_ADDRESS=", "GMAIL_APP_PASSWORD="))]
    lines.extend((f"GMAIL_ADDRESS={email}", f"GMAIL_APP_PASSWORD={password}"))
    fd, name = tempfile.mkstemp(prefix=".env.", dir=env_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(name, env_path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print("Gmail connected. Forwarded work will be ticked off within 10 minutes of arriving.")


if __name__ == "__main__":
    main()
