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
- Ошибка Telegram: `502` + `{"detail":"Telegram error"}`.

## Важные инварианты
- Без валидного `X-Alerts-Token` отправка в Telegram невозможна.
- Сервис не меняет содержимое полей, кроме склейки строк.
- В случае исключения из Telegram клиенту всегда отдаётся `502`.
