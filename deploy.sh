#!/bin/bash
set -euo pipefail

# SRCF webserver deploy helper.
# Run this on webserver_srcf in the app directory (e.g. /home/jbr46/scheduler).

git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
systemctl --user restart scheduler

# Timer that ticks off emailed work (does nothing until configure_gmail.py is run).
mkdir -p ~/.config/systemd/user
cp systemd/scheduler-mail-sync.service systemd/scheduler-mail-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now scheduler-mail-sync.timer
