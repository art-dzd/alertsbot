# alertsbot

Мини‑сервис для отправки уведомлений в Telegram. Используется как прокладка между
приложениями и чатами, без бизнес‑логики.

Сервис хранит историю в SQLite, дедуплицирует повторы по `event_id`, повторяет
доставку в Telegram при временных сбоях, поддерживает несколько Telegram-прокси
и отдаёт `/healthz` и `/readyz`. На проде работает systemd-юнит с сокетом, выпуск
идёт через Gitea CI; пересыльщик истории во вкладку tpanel — `scripts/tab_forwarder.py`.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn alertsbot.app:app --host 0.0.0.0 --port 9100
```

Если Telegram недоступен напрямую (блокировки/ограничения), можно указать прокси
для Telegram API (`TELEGRAM_PROXY_URL`) или их упорядоченный список
(`TELEGRAM_PROXY_URLS`).

## Эндпоинты

- `GET /healthz` — health‑check.
- `GET /readyz` — проверка конфигурации и истории.
- `POST /notify` — отправка уведомления.

### Формат запроса

Заголовок: `X-Alerts-Token: <ALERTS_TOKEN>`

```json
{
  "service": "Название вашего сервиса",
  "title": "Ошибки обработки",
  "message": "Текст уведомления",
  "details": "Дополнительно (опционально)"
}
```

## Документация

- [AGENTS.md](AGENTS.md) — правила, контракт и эксплуатация.
- [docs/architecture.md](docs/architecture.md) — устройство и поток доставки.
- [docs/design/notify-flow.md](docs/design/notify-flow.md) — контракт `/notify`.
- [docs/deploy.md](docs/deploy.md) — выпуск, systemd и откат.
- [docs/testing.md](docs/testing.md) — проверки и приёмка.
- [docs/commands.md](docs/commands.md) — команды.
