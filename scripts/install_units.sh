#!/usr/bin/env bash
# Ставит юниты из репо и перезапускает сервис; alertsbot.socket держит порт на время рестарта.
set -euo pipefail
cd "$(dirname "$0")/.."

sudo cp systemd/alertsbot.service systemd/alertsbot.socket /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable alertsbot.socket alertsbot.service
if ! systemctl is-active --quiet alertsbot.socket; then
  # Переход со старого юнита: порт занят процессом, сокет встаёт после его остановки.
  sudo systemctl stop alertsbot.service
  sudo systemctl start alertsbot.socket
fi
sudo systemctl restart alertsbot.service
