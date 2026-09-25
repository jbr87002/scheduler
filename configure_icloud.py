"""Run on webserver_srcf to connect iCloud without putting a password in a command or chat."""

from getpass import getpass
from pathlib import Path
import os
import re
import subprocess
import tempfile

from icloud_availability import ICloudUnavailable, check_connection


def main():
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        raise SystemExit("Run this from the deployed scheduler directory with its .env file.")
    current = env_path.read_text()
    existing_email = next(
        (match.group(1) for line in current.splitlines()
         if (match := re.fullmatch(r"ICLOUD_APPLE_ID=(.*)", line))),
        "",
    )
    email = input(f"Apple Account email [{existing_email}]: ").strip() or existing_email
    password = getpass("Apple app-specific password: ").strip()
    if not email or not password or "\n" in email or "\n" in password:
        raise SystemExit("Email and app-specific password are required.")
    os.environ["ICLOUD_APPLE_ID"] = email
    os.environ["ICLOUD_APP_PASSWORD"] = password
    print("Checking access to Work, Benji work and Our leisure…")
    try:
        check_connection()
    except ICloudUnavailable as exc:
        raise SystemExit(str(exc)) from None

    lines = [line for line in current.splitlines()
             if not line.startswith(("ICLOUD_APPLE_ID=", "ICLOUD_APP_PASSWORD="))]
    lines.extend((f"ICLOUD_APPLE_ID={email}", f"ICLOUD_APP_PASSWORD={password}"))
    fd, name = tempfile.mkstemp(prefix=".env.", dir=env_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(name, env_path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    subprocess.run(["systemctl", "--user", "restart", "scheduler"], check=True)
    print("iCloud connected and scheduler restarted.")


if __name__ == "__main__":
    main()
