#!/usr/bin/env bash
set -euo pipefail

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
else
  .venv/bin/pip install -r requirements.txt
fi

sudo cp systemd/alertsbot.service /etc/systemd/system/alertsbot.service
sudo systemctl daemon-reload
sudo systemctl enable alertsbot
sudo systemctl restart alertsbot
