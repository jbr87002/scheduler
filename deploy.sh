#!/bin/bash
set -euo pipefail

# SRCF webserver deploy helper.
# Run this on webserver_srcf in the app directory (e.g. /home/jbr46/scheduler).

git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
systemctl --user restart scheduler

