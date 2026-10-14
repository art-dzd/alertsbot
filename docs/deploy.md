# Деплой alertsbot

Канонический путь — push в main: Gitea Actions проверяет ruff, mypy и тесты,
затем автоматически выкладывает на mts. Прод: `/alertsbot`, пользователь/юнит
`alertsbot`, Python 3.10.12, runner на том же хосте. Push требует разрешения владельца.

## Выпуск

CI держит отдельный venv и под flock выполняет:

1. Запоминает предыдущий SHA, устанавливает целевой git HEAD и зависимости.
2. Копирует systemd-юнит, daemon-reload, restart.
3. Запоминает InvocationID нового запуска, проверяет health (до 10 попыток), active
   и отсутствие перезапуска между проверками.
4. Читает только журнал нового InvocationID. `scripts/check_startup.py` требует
   корректный JSON, `startup` с целевой версией и отсутствие ERROR/CRITICAL.
   Версия в приложении и CI — `git rev-parse --short=12 HEAD`.
5. Любой сбой вызывает существующий откат: предыдущий git HEAD, зависимости/юнит,
   daemon-reload, restart и healthcheck. Журнал выводится при провале job.

## Данные и настройки

`.env` уже содержит заполненные секреты и `ALERTS_APP_HOST`/`ALERTS_APP_PORT` для
uvicorn. Settings host/port не читает. Продовый `.env` не изменяется при выкладке.
`ALERTS_ENV=prod` запрещает старт с пустыми/placeholder секретами.

Новый юнит: `StateDirectory=alertsbot`, `StateDirectoryMode=0700`, `UMask=0077`.
systemd создаёт `/var/lib/alertsbot` для пользователя alertsbot. По умолчанию
`ALERTS_DB_PATH=/var/lib/alertsbot/alerts.db`; если `.env` задаёт путь, он имеет
приоритет над Environment юнита и должен быть доступен пользователю alertsbot.
На существующем хосте добавлять настройку в `.env` для стандартного пути не требуется.
Схема SQLite создаётся приложением при первом доступе, без отдельного мигратора.
Данные хранятся 90 дней и переживают git reset/деплой/откат.

## Проверки на хосте

```bash
systemctl status alertsbot --no-pager
curl -fsS http://127.0.0.1:9100/healthz
curl -fsS http://127.0.0.1:9100/readyz
journalctl -u alertsbot -n 100 --no-pager -o cat
diff /etc/systemd/system/alertsbot.service /alertsbot/systemd/alertsbot.service
```

Живость: `healthz` → ok. Готовность: `readyz` → ready (конфиг и БД; не Telegram).
Обязательно startup с SHA выпуска, отсутствие ERROR до записи startup, установленный
юнит совпадает с репо. Чтение последних алертов через python3+sqlite3 — [AGENTS.md](../AGENTS.md).
Политика journald на хосте должна хранить JSON-логи минимум 14 дней; репо её не меняет.

## Резервный путь и откат

`bash scripts/restart.sh` создаёт/обновляет venv, устанавливает юнит и рестартит
сервис. Это резервный путь при недоступном CI, без параллельной выкладки.
После него те же post-check обязательны вручную.

Ручной откат по разрешению: `sudo -u alertsbot git -C /alertsbot reset --hard <SHA>`,
затем `bash scripts/restart.sh` и post-check. SQLite не удалять: старый код может
не читать историю, но файл останется для последующего обновления/разбора.
Автоматический откат проверяет health, а ручная проверка журнала отката остаётся
за оператором (старые выпуски могут писать прежний формат).
