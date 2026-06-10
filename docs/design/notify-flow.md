# Дизайн: notify flow

## Контракт входа
- Метод: `POST /notify`
- Заголовок: `X-Alerts-Token: <ALERTS_TOKEN>`
- JSON-поля:
  - `service` (string, required)
  - `title` (string, required)
  - `message` (string, required)
  - `details` (string, optional)

## Правила форматирования сообщения
1. Базовый текст собирается как:
   - строка 1: `service`
   - строка 2: `title`
   - строка 3: `message`
2. Если передан `details`, он добавляется отдельным блоком после пустой строки.

Пример:
```text
billing-api
Ошибки обработки
5xx rate > 10%

trace_id=abc123
```

## Ответ API
- Успех: `200` + `{ "status": "sent" }`.
- Ошибка авторизации: `401` + `{"detail":"Unauthorized"}`.
- Telegram отклонил payload: `422` + `{"detail":"Telegram payload rejected"}`.
- Постоянная ошибка Telegram-доступа/конфига: `424` + `{"detail":"Permanent Telegram error"}`.
- Временная ошибка Telegram: `502` + `{"detail":"Temporary Telegram error"}`.

## Важные инварианты
- Без валидного `X-Alerts-Token` отправка в Telegram невозможна.
- Сервис склеивает поля в текст и усекает итоговое сообщение до лимита Telegram.
- `429` от Telegram ретраится один раз с ограниченной паузой по `retry_after`.
- `400` считается ошибкой payload, `401/403/404` — постоянной ошибкой доступа/конфига.
