# Анализ тестового покрытия: alertsbot

Дата: 2026-03-20

---

## Текущее состояние

**Автотестов: 0.** Ни одного файла `test_*.py`, `*_test.py`, `conftest.py`. `pytest` отсутствует в `requirements.txt`. Нет `pytest.ini`, `setup.cfg`, `pyproject.toml` с секцией `[tool.pytest]`.

Единственный quality gate — ручной smoke-test через curl (описан в `docs/testing.md`).

---

## Покрытые модули

Нет.

---

## Непокрытые модули

### `alertsbot/app.py` — Риск: ВЫСОКИЙ

Ядро сервиса: HTTP-роутинг, авторизация, форматирование, оркестрация Telegram-вызова. 7 логических веток без единого теста.

| Ветка | Строки | Что проверить | Риск |
|---|---|---|---|
| `GET /healthz` → 200 | 29–33 | Возвращает `{"status":"ok"}` | Низкий (тривиально) |
| `POST /notify` без заголовка → 401 | 43–44 | `X-Alerts-Token` отсутствует | Высокий (авторизация) |
| `POST /notify` с неверным токеном → 401 | 43–44 | Токен не совпал | Высокий (авторизация) |
| `POST /notify` с пустым `ALERTS_TOKEN` → 401 | 43 | `not settings.alerts_token` — сервис блокирует всё | Высокий (edge case) |
| `POST /notify` валидный, без details → 200 | 46, 50–57 | Текст: `service\ntitle\nmessage` | Средний |
| `POST /notify` валидный, с details → 200 | 46–48, 50–57 | Текст: `service\ntitle\nmessage\n\ndetails` | Средний |
| `POST /notify` Telegram ошибка → 502 | 58–60 | `send_message` бросает исключение | Высокий (error path) |
| Невалидный JSON / отсутствуют поля → 422 | pydantic | FastAPI-валидация `NotifyRequest` | Средний |

### `alertsbot/config.py` — Риск: СРЕДНИЙ

Конфигурация: загрузка из env, дефолты, кэширование.

| Ветка | Строки | Что проверить | Риск |
|---|---|---|---|
| Загрузка из переменных окружения | 11–26 | `Settings()` с env vars | Средний |
| Дефолтные значения | 16–26 | Все дефолты корректны | Низкий |
| Типизация: `app_port` как int | 21 | `"abc"` → ValidationError | Низкий |
| `@lru_cache` возвращает один объект | 29–33 | `get_settings() is get_settings()` | Низкий |
| `.env` файл подхватывается | 14 | `env_file=".env"` работает | Средний |

### `alertsbot/telegram.py` — Риск: ВЫСОКИЙ

Единственная точка интеграции с внешним API.

| Ветка | Строки | Что проверить | Риск |
|---|---|---|---|
| Успешная отправка | 17–28 | POST на правильный URL, правильный payload | Высокий |
| Telegram 4xx → `httpx.HTTPStatusError` | 28 | `raise_for_status()` при ошибке API | Высокий |
| Таймаут → `httpx.TimeoutException` | 23, 26 | Timeout корректно прокидывается | Средний |
| DNS/сеть → `httpx.ConnectError` | 26 | Сетевая ошибка | Средний |
| Proxy передаётся в клиент | 24–25 | `proxy_url` → `client_kwargs["proxy"]` | Средний |
| Без proxy → нет ключа `proxy` | 24 | Пустая строка → нет proxy | Низкий |
| `trust_env=False` | 23 | Не подхватывает env-прокси хоста | Низкий |
| `disable_web_page_preview: True` | 21 | Параметр передаётся | Низкий |

---

## Качество существующих тестов

Не применимо — тестов нет.

---

## Приоритетный план написания тестов

### Инструментарий

```
pytest + httpx (для TestClient FastAPI) + respx или pytest-httpx (для мока httpx-запросов)
```

Добавить в `requirements-dev.txt`:
```
pytest
httpx
respx
```

### Фаза 1 — Критический путь (5 тестов, ~1 час)

Покрывают все HTTP-ответы сервиса. Минимум, без которого нельзя деплоить с уверенностью.

| # | Тест | Модуль | Ожидание |
|---|---|---|---|
| 1 | `test_healthz_returns_ok` | app | `GET /healthz` → 200, `{"status":"ok"}` |
| 2 | `test_notify_without_token_returns_401` | app | `POST /notify` без заголовка → 401 |
| 3 | `test_notify_wrong_token_returns_401` | app | `POST /notify` с неверным токеном → 401 |
| 4 | `test_notify_success` | app + telegram (mock) | Валидный запрос → 200, `{"status":"sent"}` |
| 5 | `test_notify_telegram_error_returns_502` | app + telegram (mock) | `send_message` бросает → 502 |

**Техника:** Использовать `app.dependency_overrides[get_settings]` для подстановки тестовых настроек. Мокать `send_message` через `respx` или `unittest.mock.patch`.

### Фаза 2 — Форматирование и валидация (4 теста, ~30 мин)

| # | Тест | Модуль | Ожидание |
|---|---|---|---|
| 6 | `test_notify_text_format_without_details` | app | Telegram получает `"svc\ntitle\nmsg"` |
| 7 | `test_notify_text_format_with_details` | app | Telegram получает `"svc\ntitle\nmsg\n\ndetails"` |
| 8 | `test_notify_missing_required_field_returns_422` | app | Без `service` → 422 |
| 9 | `test_notify_empty_alerts_token_blocks_all` | app | `ALERTS_TOKEN=""` → любой запрос 401 |

### Фаза 3 — Telegram-клиент изолированно (4 теста, ~30 мин)

| # | Тест | Модуль | Ожидание |
|---|---|---|---|
| 10 | `test_send_message_posts_correct_url` | telegram | URL = `https://api.telegram.org/bot<token>/sendMessage` |
| 11 | `test_send_message_posts_correct_payload` | telegram | JSON содержит `chat_id`, `text`, `disable_web_page_preview` |
| 12 | `test_send_message_raises_on_4xx` | telegram | Telegram 403 → `httpx.HTTPStatusError` |
| 13 | `test_send_message_with_proxy` | telegram | `proxy_url="http://proxy"` → `proxy` в kwargs клиента |

### Фаза 4 — Конфигурация (3 теста, ~20 мин)

| # | Тест | Модуль | Ожидание |
|---|---|---|---|
| 14 | `test_settings_defaults` | config | Все дефолты совпадают с `.env.example` |
| 15 | `test_settings_from_env_vars` | config | `os.environ` → корректные значения |
| 16 | `test_get_settings_cached` | config | `get_settings() is get_settings()` |

---

## Сводка

| Метрика | Значение |
|---|---|
| Тестовых файлов | 0 |
| Тестов | 0 |
| Покрытие (оценка) | 0% |
| Логических веток в коде | ~20 |
| Тестов для полного покрытия веток | 16 |
| Фаза 1 (критический путь) | 5 тестов, ~1 час |
| Фазы 1–4 (полное покрытие) | 16 тестов, ~2.5 часа |

**Главный риск:** авторизация (`app.py:43-44`) и обработка ошибок Telegram (`app.py:58-60`) — самые важные ветки, и обе без тестов. Одна ошибка в рефакторинге auth-проверки = открытый endpoint в проде.
