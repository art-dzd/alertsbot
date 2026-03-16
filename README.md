# alertsbot

Мини‑сервис для отправки уведомлений в Telegram. Используется как прокладка между
приложениями и чатами, без бизнес‑логики.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn alertsbot.app:app --host 0.0.0.0 --port 9100
```

Если Telegram недоступен напрямую (блокировки/ограничения), можно указать прокси для Telegram API:

```bash
TELEGRAM_PROXY_URL=http://127.0.0.1:7890
```

## Эндпоинты

- `GET /healthz` — health‑check.
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
