# Деплой alertsbot

## Способ деплоя

Канонический путь — **push в `main`**: Gitea Actions (`.gitea/workflows/ci-deploy.yml`)
прогоняет ruff + pytest и автоматически деплоит на прод.
Прод — хост `mts`, каталог `/alertsbot`, systemd-юнит `alertsbot`, runner живёт на самом хосте.
Docker-контейнеров и миграций БД нет — проект stateless.

Руками на проде — только верификация. `scripts/restart.sh` — резервный путь
на случай недоступности CI; не катать руками поверх работающего CI (гонка).

## Что делает CI-деплой

1. Job `ci`: `ruff check` + `pytest` в отдельном CI-venv на runner'е.
2. Job `deploy` (только после зелёного `ci`, под flock):
   `git reset --hard` на SHA пуша → `pip install -r requirements.txt` →
   установка systemd-юнита из `systemd/alertsbot.service` → `daemon-reload` →
   `restart` → healthcheck (до 10 попыток). При сбое — автоматический откат
   на предыдущий SHA с тем же циклом.

## Что нужно заранее (новый хост)

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

## Резервный ручной деплой (systemd)

`scripts/restart.sh`:
1. Создаёт/обновляет `.venv` и зависимости.
2. Копирует `systemd/alertsbot.service` в `/etc/systemd/system/`.
3. Выполняет `daemon-reload`, `enable`, `restart`.

```bash
cd /alertsbot
bash scripts/restart.sh
```

## Проверки после деплоя (на проде)

```bash
systemctl status alertsbot --no-pager
curl -fsS http://127.0.0.1:9100/healthz
curl -fsS http://127.0.0.1:9100/readyz
journalctl -u alertsbot -n 100 --no-pager
diff /etc/systemd/system/alertsbot.service /alertsbot/systemd/alertsbot.service
```

Ожидаемое состояние:
- юнит `alertsbot` в статусе `active (running)`;
- `/healthz` отвечает `{"status":"ok"}`, `/readyz` — `{"status":"ready"}`;
- установленный юнит совпадает с юнитом в репозитории;
- в логах нет повторяющихся ошибок Telegram/авторизации.

## Обновление

- Запушить в `main`, дождаться зелёного CI.
- Выполнить post-check из раздела выше.

## Откат

- При неудачном healthcheck CI откатывается сам на предыдущий SHA.
- Ручной откат: `sudo -u alertsbot git -C /alertsbot reset --hard <SHA>`,
  затем `bash scripts/restart.sh` и post-check.
