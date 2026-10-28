#!/usr/bin/env bash
# Ставит юниты из репо и перезапускает сервис; alertsbot.socket держит порт на время рестарта.
set -euo pipefail
cd "$(dirname "$0")/.."

sudo visudo -cf sudoers/alertsbot-history
sudo install -d -m 0755 /usr/local/libexec
sudo install -m 0755 scripts/read_history.py /usr/local/libexec/alertsbot-read-history.tmp
sudo mv -f /usr/local/libexec/alertsbot-read-history.tmp /usr/local/libexec/alertsbot-read-history
sudo install -m 0440 sudoers/alertsbot-history /etc/sudoers.d/alertsbot-history.tmp
sudo mv -f /etc/sudoers.d/alertsbot-history.tmp /etc/sudoers.d/alertsbot-history

sudo cp systemd/alertsbot.service systemd/alertsbot.socket /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable alertsbot.socket alertsbot.service
if ! systemctl is-active --quiet alertsbot.socket; then
  # Переход со старого юнита: порт занят процессом, сокет встаёт после его остановки.
  sudo systemctl stop alertsbot.service
  sudo systemctl start alertsbot.socket
fi
sudo systemctl restart alertsbot.service
