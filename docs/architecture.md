# Архитектура alertsbot

## Назначение

`alertsbot` — минимальный HTTP-сервис, который принимает уведомления от внутренних приложений и пересылает их в Telegram-чат.
Сервис сознательно без бизнес-логики: только авторизация, форматирование текста и доставка в Telegram Bot API.
Состояние не хранит, очередей и ретраев нет.

## Стек технологий

| Компонент       | Версия / библиотека          |
|-----------------|------------------------------|
| Язык            | Python 3.11+                 |
| Web-фреймворк   | FastAPI                      |
| ASGI-сервер     | Uvicorn (с `standard`-extras)|
| HTTP-клиент     | httpx                        |
| Валидация       | pydantic 2.x                 |
| Конфигурация    | pydantic-settings            |
| Процесс-менеджер| systemd                      |

## Модули и их роли

| Модуль                  | Роль                                                                            |
|-------------------------|---------------------------------------------------------------------------------|
| `alertsbot/app.py`      | FastAPI-приложение: модель `NotifyRequest`, маршруты `/healthz` и `/notify`, авторизация и форматирование текста. |
| `alertsbot/config.py`   | Загрузка настроек из `.env` / переменных окружения через `pydantic-settings` (`Settings`, `get_settings()`). |
| `alertsbot/telegram.py` | Асинхронный `httpx`-клиент для `sendMessage` в Telegram Bot API (опциональный прокси, `trust_env=False`). |
| `alertsbot/__init__.py`  | Пустой пакетный init.                                                           |

## Зависимости между модулями

```
 ┌──────────────┐
 │   app.py     │  ← точка входа (FastAPI)
 │  /healthz    │
 │  /notify     │
 └──┬───────┬───┘
    │       │
    │ import│ import
    ▼       ▼
┌────────┐ ┌──────────────┐
│config.py│ │ telegram.py  │
│Settings │ │ send_message │
└────────┘ └──────────────┘
     ▲              │
     │              │ httpx.AsyncClient
     │              ▼
     │     ┌──────────────────┐
     │     │ Telegram Bot API │
     │     └──────────────────┘
  .env / ENV
```

- `app.py` импортирует `config.get_settings()` и `telegram.send_message()`.
- `config.py` и `telegram.py` независимы друг от друга.
- `telegram.py` не знает о конфигурации — получает параметры через аргументы.

## Точки входа (API endpoints)

| Метод  | Путь       | Авторизация       | Описание                        |
|--------|------------|-------------------|---------------------------------|
| `GET`  | `/healthz` | нет               | Liveness-проверка. Возвращает `{"status":"ok"}`. |
| `POST` | `/notify`  | `X-Alerts-Token`  | Приём уведомления и отправка в Telegram. |

### POST /notify — контракт

**Заголовок:** `X-Alerts-Token: <ALERTS_TOKEN>`

**Тело (JSON):**

| Поле      | Тип    | Обязательное | Описание                 |
|-----------|--------|:------------:|--------------------------|
| `service` | string | да           | Название сервиса         |
| `title`   | string | да           | Заголовок уведомления    |
| `message` | string | да           | Основной текст           |
| `details` | string | нет          | Дополнительные детали    |

**Ответы:**

| Код  | Тело                            | Когда                              |
|------|---------------------------------|------------------------------------|
| 200  | `{"status":"sent"}`             | Сообщение успешно отправлено       |
| 401  | `{"detail":"Unauthorized"}`     | Токен отсутствует или не совпал    |
| 502  | `{"detail":"Telegram error"}`   | Telegram API недоступен или ошибка |

## Data flow

```
Клиент (внутренний сервис)
   │
   │  POST /notify  {service, title, message, details?}
   │  Header: X-Alerts-Token
   ▼
┌──────────────────────────────────────────────────┐
│  app.py                                          │
│  1. Проверить X-Alerts-Token == ALERTS_TOKEN     │
│     └─ нет → 401 Unauthorized                    │
│  2. Собрать текст:                               │
│     "{service}\n{title}\n{message}"              │
│     + "\n\n{details}" если передан               │
│  3. Вызвать send_message(token, chat_id, text,   │
│                          timeout, proxy_url)      │
│     └─ исключение → лог + 502 Telegram error     │
│  4. Вернуть {"status":"sent"}                    │
└──────────────────────────────────────────────────┘
   │
   │  httpx POST (async, trust_env=False)
   │  опционально через TELEGRAM_PROXY_URL
   ▼
┌──────────────────────────────────────────────────┐
│  Telegram Bot API                                │
│  POST /bot<TOKEN>/sendMessage                    │
│  {chat_id, text, disable_web_page_preview: true} │
└──────────────────────────────────────────────────┘
   │
   ▼
  Telegram-чат (ALERTS_CHAT_ID)
```

## Конфигурация (env)

Все настройки читаются из `.env` или переменных окружения (см. `.env.example`).

| Переменная                       | По умолчанию | Описание                                    |
|----------------------------------|:------------:|---------------------------------------------|
| `ALERTS_BOT_TOKEN`               | —            | Токен Telegram-бота                         |
| `ALERTS_CHAT_ID`                 | —            | Целевой чат/канал                           |
| `ALERTS_TOKEN`                   | —            | Общий секрет для клиентов `/notify`         |
| `ALERTS_APP_HOST`                | `0.0.0.0`   | Host для uvicorn                            |
| `ALERTS_APP_PORT`                | `9100`       | Порт для uvicorn                            |
| `ALERTS_LOG_LEVEL`               | `INFO`       | Уровень логирования                         |
| `ALERTS_REQUEST_TIMEOUT_SECONDS` | `10.0`       | Timeout исходящего запроса в Telegram (сек)  |
| `TELEGRAM_PROXY_URL`             | `""`         | HTTP/SOCKS-прокси для Telegram API (опционально) |

## Запуск

### Локально (dev)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить секреты
uvicorn alertsbot.app:app --host 0.0.0.0 --port 9100
```

### Продакшн (systemd)

Юнит: `systemd/alertsbot.service` — запускает uvicorn с параметрами из `.env`.

```
WorkingDirectory=/alertsbot
EnvironmentFile=/alertsbot/.env
ExecStart=/alertsbot/.venv/bin/uvicorn alertsbot.app:app --host ${ALERTS_APP_HOST} --port ${ALERTS_APP_PORT}
Restart=always, RestartSec=5
```

Деплой одной командой:
```bash
bash scripts/restart.sh
```

`restart.sh` создаёт/обновляет `.venv`, ставит зависимости, копирует юнит в systemd, делает `daemon-reload` → `enable` → `restart`.

## Наблюдаемость и ограничения

- Логи через стандартный `logging` (`logger = logging.getLogger("alertsbot")`), уровень задаётся через `ALERTS_LOG_LEVEL`.
- Повторные отправки не дедуплицируются — идемпотентность на стороне клиента.
- Ретраи/очереди не реализованы: при сетевых проблемах клиент получает `502`.
- Docker не используется — только systemd.
