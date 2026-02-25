# Архитектура alertsbot

## Назначение
`alertsbot` — минимальный HTTP-сервис, который принимает уведомления от внутренних приложений и пересылает их в Telegram-чат.
Сервис сознательно без бизнес-логики: только авторизация, форматирование текста и доставка в Telegram Bot API.

## Границы системы
- Вход: `POST /notify` с JSON-телом и заголовком `X-Alerts-Token`.
- Выход: `https://api.telegram.org/bot<TOKEN>/sendMessage`.
- Внутреннее состояние: отсутствует (БД/очереди нет).

## Компоненты
- `alertsbot/app.py`: FastAPI-приложение, модель запроса, маршруты `/healthz` и `/notify`.
- `alertsbot/config.py`: загрузка настроек из окружения через `pydantic-settings`.
- `alertsbot/telegram.py`: асинхронный HTTP-клиент `httpx` для отправки сообщения в Telegram.

## Поток обработки `/notify`
1. Клиент отправляет JSON с полями `service`, `title`, `message`, опционально `details`.
2. API проверяет заголовок `X-Alerts-Token` против `ALERTS_TOKEN`.
3. Сервис собирает итоговый текст в фиксированном формате.
4. `send_message(...)` вызывает Telegram Bot API методом `sendMessage`.
5. При успехе API возвращает `{ "status": "sent" }`.

## Контракт ошибок
- `401 Unauthorized`: нет токена или токен не совпал.
- `502 Telegram error`: Telegram API вернул ошибку или недоступен.
- `GET /healthz` всегда возвращает `{ "status": "ok" }`, если процесс жив.

## Конфигурация (env)
- `ALERTS_BOT_TOKEN`: токен Telegram-бота.
- `ALERTS_CHAT_ID`: целевой чат/канал.
- `ALERTS_TOKEN`: общий секрет для клиентов `POST /notify`.
- `ALERTS_APP_HOST`: host для uvicorn (по умолчанию `0.0.0.0`).
- `ALERTS_APP_PORT`: порт для uvicorn (по умолчанию `9100`).
- `ALERTS_LOG_LEVEL`: уровень логирования.
- `ALERTS_REQUEST_TIMEOUT_SECONDS`: timeout исходящего запроса в Telegram.

## Наблюдаемость и ограничения
- Логи пишутся через стандартный `logging` (`logger = logging.getLogger("alertsbot")`).
- Повторные отправки не дедуплицируются: идемпотентность на стороне клиента.
- Ретраи/очереди не реализованы: при сетевых проблемах клиент получает `502`.
