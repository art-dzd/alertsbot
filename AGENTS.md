# alertsbot

Небольшой FastAPI-сервис на `mts`: принимает `POST /notify`, проверяет
`X-Alerts-Token`, сохраняет историю и синхронно отправляет сообщение в Telegram.
Python 3.10+, stdlib SQLite, существующие FastAPI/httpx/pydantic. Один воркер.

## Правила

- Общаться по-русски. Минимальные изменения, без новых зависимостей и очередей.
- До работы читать скиллы `lean-code`, `logging`, `testing`.
- Работать в `main`; коммитить только свои файлы, сообщения коммитов по-русски.
- Push в `main` автоматически выкладывает на прод: только с разрешения владельца.
- Реальные сообщения в Telegram, SSH и изменения продового `.env` — только по разрешению.
- Секреты, тела отклонённых запросов и тексты исключений не выводить в журнал.
- Разовые отчёты и выгрузки держать в `/tmp`, в `docs/` — рабочая документация.

## Контракт

Авторизация идёт до чтения тела. Любой JSON-объект нормализуется и отправляется:
пропущенные поля заменяются заглушками, нестроковые приводятся к JSON-строке,
длинные обрезаются для Telegram. Полный текст остаётся в истории.
Необъект/невалидный JSON получает `422` и строку `rejected`; неверный токен — `401`
без записи истории. Telegram: успех `200 sent`, payload `422`, постоянный сбой
`424`, временный `502` для повторов клиента. Доставка остаётся внутри запроса.

Кэш `event_id`: один час, максимум 10 000 событий; успех и постоянные ошибки
повторяются без отправки, `502` не кэшируется. Кэш живёт до рестарта.
Каждый авторизованный запрос, включая повтор, получает отдельную строку истории.
Сбой SQLite логируется как ERROR и не мешает отправке.

## Эксплуатация

Прод: `/alertsbot`, пользователь/юнит `alertsbot`. systemd создаёт
`/var/lib/alertsbot` с правами `0700`; история по умолчанию —
`/var/lib/alertsbot/alerts.db` (`ALERTS_DB_PATH`). Хранение 5 лет, чистка при
старте и на запросах не чаще раза в час. `pending` означает незавершённую обработку,
`sent` — успех, `failed` — сбой, `rejected` — отклонённое тело.

Логи: JSON в stdout → journald. Читать:

```bash
journalctl -u alertsbot -n 100 --no-pager -o cat
journalctl -u alertsbot -f -o cat
```

Последние 20 алертов, включая текст (SQLite CLI не нужен):

```bash
sudo -u alertsbot python3 - <<'PY'
import json
import sqlite3
from contextlib import closing
with closing(sqlite3.connect('file:/var/lib/alertsbot/alerts.db?mode=ro', uri=True)) as db:
    db.row_factory = sqlite3.Row
    for row in db.execute('SELECT * FROM alerts ORDER BY created_at DESC, id DESC LIMIT 20'):
        print(json.dumps(dict(row), ensure_ascii=False))
PY
```

Health: `curl -fsS http://127.0.0.1:9100/healthz` — живость;
`curl -fsS http://127.0.0.1:9100/readyz` — конфигурация и доступность истории.
Telegram health не вызывает. Метрик ресурсов внутри сервиса нет: их собирают на хосте.
Сервис сам себе алерты не отправляет; сбои доставки/истории видны в ERROR и истории.
Journald должен сохранять рабочие логи минимум 14 дней; политика задаётся на хосте.

## Пересыльщик во вкладку (web)

`scripts/tab_forwarder.py` раз в минуту от пользователя server читает историю с mts
по ssh (сначала напрямую по tailnet, при недоступности — через vpn) и SQLite `mode=ro`;
отправляет одну сводку через `tpanel say`. Перед чтением проверяет `readyz` alertsbot
на mts, после — внешний путь `https://aiomc.ru/alerts/readyz` (им шлют vpn, hr, gitea):
канал (mts, alertsbot, история или внешний путь) недоступен дольше 3 минут — одно сообщение
во вкладку, после восстановления — второе. Состояние сбоя — файл `outage`.
Токен tpanel остаётся на web. Вкладка — `ALERTS_TAB`, по умолчанию `admin/alerts`.
Первый запуск сохраняет текущий max(id) без отправки старой истории. Позиция —
`/var/lib/alerts-tab-forwarder/last_id`, меняется атомарно после доставки; cached
пропускается. Свежий pending задерживает следующие строки, после пяти минут
показывается с неизвестным итогом. Лимит — 100 строк и 6000 символов за проход;
не вместившиеся алерты уходят следующей сводкой (позиция — до последнего показанного).
История пересоздана (max(id) меньше позиции) — чтение с начала и WARNING `history_reset`.

Установка на web из корня репо:

```bash
sudo install -m 0755 scripts/tab_forwarder.py /usr/local/bin/alerts-tab-forwarder
sudo install -m 0644 systemd/web/alerts-tab-forwarder.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alerts-tab-forwarder.timer
journalctl -u alerts-tab-forwarder -n 100 --no-pager -o cat
```

Успешные проходы, включая пустые, обновляют пульс
`alerts_tab_forwarder_last_success_timestamp_seconds` в
`/var/lib/prometheus/node-exporter/alerts_tab_forwarder.prom` (root, 0644).
Пустые проходы не логируются; работа — один JSON-итог, сбои — ERROR без текста
алертов и stderr. Версия в журнале — первые 12 символов SHA256 скрипта.
Подробности установки и остановки — [docs/deploy.md](docs/deploy.md).

## Документация и проверки

- [Архитектура](docs/architecture.md)
- [Контракт /notify](docs/design/notify-flow.md)
- [Тестирование](docs/testing.md)
- [Деплой и откат](docs/deploy.md)
- [Команды](docs/commands.md)

```bash
.venv/bin/python -m pytest -q --tb=short
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
```

Системный юнит: `systemd/alertsbot.service`. Канонический выпуск — Gitea CI;
`bash scripts/restart.sh` — резервный путь. После рестарта обязательны health,
`startup` с SHA выпуска и отсутствие ERROR до неё.
