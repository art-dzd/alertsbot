# Деплой alertsbot

## Способ деплоя

Ручной деплой через `scripts/restart.sh` + systemd. CI/CD-пайплайнов, Docker-контейнеров и миграций БД нет — проект stateless.

## Что нужно заранее
- Linux-хост с `systemd` и доступом в интернет к Telegram API (или настроенный `TELEGRAM_PROXY_URL`).
- Python 3.11+ и `python3-venv`.
- Заполненный `.env` на основе `.env.example`.

## Первый запуск вручную
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn alertsbot.app:app --host 0.0.0.0 --port 9100
```

## Установка как systemd-сервис
В проекте есть скрипт `scripts/restart.sh`, который:
1. Создаёт/обновляет `.venv` и зависимости.
2. Копирует `systemd/alertsbot.service` в `/etc/systemd/system/`.
3. Выполняет `daemon-reload`, `enable`, `restart`.

Запуск:
```bash
cd /alertsbot
bash scripts/restart.sh
```

## Проверки после деплоя
```bash
systemctl status alertsbot --no-pager
curl -fsS http://127.0.0.1:9100/healthz
journalctl -u alertsbot -n 100 --no-pager
```

Ожидаемое состояние:
- юнит `alertsbot` в статусе `active (running)`;
- `/healthz` отвечает `{"status":"ok"}`;
- в логах нет повторяющихся ошибок Telegram/авторизации.

## Обновление
- Обнови код в рабочей директории.
- Повтори `bash scripts/restart.sh`.
- Выполни post-check из раздела выше.

## Откат
- Верни предыдущую рабочую ревизию кода.
- Повтори `bash scripts/restart.sh`.
- Снова проверь `systemctl status`, `healthz` и логи.
