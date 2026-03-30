# Code Review: alertsbot

Полное ревью всех модулей проекта. Дата: 2026-03-20.

---

## Находки

### MEDIUM | `app.py:24` | Settings инициализируются на уровне модуля — до старта event loop

```python
settings = get_settings()          # строка 24 — вызов при импорте модуля
logging.basicConfig(level=settings.log_level)  # строка 25
```

`get_settings()` выполняется при `import alertsbot.app`, т.е. **на этапе загрузки модуля**, ещё до запуска uvicorn. Если `.env` отсутствует или содержит невалидные значения, ошибка произойдёт при импорте — без нормального HTTP-ответа и без возможности обработать это в middleware/lifespan. Кроме того, `logging.basicConfig` вызывается один раз на уровне модуля, что может конфликтовать с настройками uvicorn-логгера.

**Рекомендация:** Перенести инициализацию в `lifespan` контекст FastAPI или оставить как есть — для такого простого сервиса это приемлемо, но стоит понимать ограничение.

---

### MEDIUM | `app.py:43` | Сервис тихо запускается с пустыми секретами

```python
if not settings.alerts_token or x_alerts_token != settings.alerts_token:
```

Если `ALERTS_TOKEN=""` (дефолт) и клиент не передал заголовок (`x_alerts_token=None`), проверка `not ""` → `True` и сразу 401. Это корректное поведение — пустой токен блокирует все запросы.

**Но:** сервис при этом стартует без ошибок — `healthz` отвечает OK, и оператор может не заметить, что `/notify` непригоден. Аналогично для пустых `ALERTS_BOT_TOKEN` и `ALERTS_CHAT_ID` — они не проверяются при старте. Ошибка вылезет только при первом реальном запросе в Telegram (пустой bot-token → 404 от Telegram API).

**Рекомендация:** Добавить startup-валидацию (в lifespan или при инициализации), логируя warning если критичные секреты пустые.

---

### MEDIUM | `telegram.py:26` | Новый httpx.AsyncClient создаётся на каждый запрос

```python
async with httpx.AsyncClient(**client_kwargs) as client:
    response = await client.post(url, json=payload)
```

На каждый `POST /notify` создаётся и закрывается новый `AsyncClient`. Это значит:
- Новое TCP-соединение к api.telegram.org на каждый запрос (нет connection pooling).
- Overhead на TLS-handshake каждый раз.

Для единичных алертов это некритично, но при всплесках (10+ уведомлений за секунду) это станет узким местом. `httpx` рекомендует переиспользовать клиента.

**Рекомендация:** Создать `AsyncClient` один раз (на уровне модуля или в lifespan) и переиспользовать. Закрывать в shutdown. Это также решит connection pooling.

---

### MEDIUM | `app.py:58-60` | Bare except глотает тип исключения в HTTP-ответе

```python
except Exception:  # noqa: BLE001
    logger.exception("Failed to send Telegram message")
    raise HTTPException(status_code=502, detail="Telegram error")
```

`logger.exception()` корректно пишет traceback в лог — исключение не теряется. `noqa: BLE001` подавляет ruff-предупреждение осознанно. Однако клиент получает одинаковый `502 Telegram error` вне зависимости от причины: таймаут, 403 от Telegram (бот заблокирован), невалидный chat_id, сетевая ошибка. Диагностика на стороне клиента невозможна.

**Рекомендация:** Для внутреннего сервиса можно добавить тип ошибки в detail: `"Telegram timeout"` / `"Telegram 403"` / `"Telegram connection error"`. Это упростит диагностику без раскрытия чувствительных данных.

---

### LOW | `config.py:16-18` | Секреты с дефолтом `""` вместо обязательного значения

```python
alerts_bot_token: str = Field(default="", alias="ALERTS_BOT_TOKEN")
alerts_chat_id: str = Field(default="", alias="ALERTS_CHAT_ID")
alerts_token: str = Field(default="", alias="ALERTS_TOKEN")
```

Все три секрета имеют `default=""` — pydantic не выбросит `ValidationError` при их отсутствии. Если убрать дефолты (сделать поля required), сервис откажется стартовать без `.env` — fail fast.

**Рекомендация:** Убрать `default=""` для трёх секретов, чтобы отсутствие `.env` ломало старт явно, а не давало тихо нерабочий сервис.

---

### LOW | `systemd/alertsbot.service:9` | ExecStart использует shell-подстановку `${VAR}` без явного shell

```
ExecStart=/alertsbot/.venv/bin/uvicorn alertsbot.app:app --host ${ALERTS_APP_HOST} --port ${ALERTS_APP_PORT}
```

systemd подставляет переменные из `EnvironmentFile` в `ExecStart`, но синтаксис `${VAR}` в systemd — это нативная подстановка (документированная), а не shell. Это работает корректно **при условии**, что переменные определены в EnvironmentFile. Если `ALERTS_APP_HOST` отсутствует в `.env`, systemd подставит пустую строку, и uvicorn упадёт с непонятной ошибкой.

**Рекомендация:** В `.env.example` и документации явно указать, что `ALERTS_APP_HOST` и `ALERTS_APP_PORT` обязательны для systemd-деплоя. Уже частично решено через дефолты в `.env.example`.

---

### LOW | `app.py:47` | `details=""` пройдёт проверку `if payload.details` как falsy

```python
if payload.details:
    text = f"{text}\n\n{payload.details}"
```

Если клиент передаст `"details": ""`, то `bool("") == False` — пустой блок details не добавится. Это корректное поведение (пустая строка = нет деталей), но неочевидно: тип поля `str | None`, а проверка ловит не только `None`, но и `""`. Если кто-то поменяет тип или уберёт `| None`, поведение тихо изменится.

**Рекомендация:** Заменить на явную проверку `if payload.details is not None` для consistency с типом, или оставить как есть — для текущего сценария оба варианта работают.

---

### LOW | `restart.sh` | Скрипт не проверяет pwd и не делает post-check

```bash
sudo systemctl restart alertsbot
# конец скрипта
```

Скрипт не проверяет, что запущен из директории проекта (наличие `requirements.txt`, `systemd/alertsbot.service`). Если запустить из другой директории — `pip install -r requirements.txt` тихо упадёт с `set -e`, но ошибка будет неочевидной. Также нет post-restart проверки (`systemctl is-active`, `curl healthz`).

**Рекомендация:** Добавить `[[ -f requirements.txt ]] || { echo "Run from project root"; exit 1; }` в начало и опциональный healthcheck после рестарта.

---

### LOW | `telegram.py:21` | `disable_web_page_preview` — deprecated параметр Telegram API

```python
"disable_web_page_preview": True,
```

В Telegram Bot API 7.0+ (декабрь 2023) параметр `disable_web_page_preview` заменён на `link_preview_options: {"is_disabled": true}`. Старый параметр пока работает для обратной совместимости, но может быть убран в будущих версиях API.

**Рекомендация:** Заменить на новый формат когда будет удобно. Не срочно — старый параметр ещё поддерживается.

---

## Отсутствующие проблемы (проверено, всё чисто)

| Категория | Статус |
|---|---|
| Race conditions | Нет shared mutable state, каждый запрос изолирован |
| Утечки ресурсов | `httpx.AsyncClient` используется через `async with` — закрывается корректно |
| SQL injection / XSS | Нет БД, нет HTML-рендеринга |
| TODO/FIXME/HACK в коде | Не найдено |
| Несоответствие API-контракта | Код и `docs/design/notify-flow.md` совпадают |

---

## Сводная таблица

| # | Severity | Файл:строка | Проблема |
|---|----------|-------------|----------|
| 1 | MEDIUM | `app.py:24` | Settings на уровне модуля, до event loop |
| 2 | MEDIUM | `app.py:43` | Сервис стартует с пустыми секретами без warning |
| 3 | MEDIUM | `telegram.py:26` | Новый httpx-клиент на каждый запрос, нет connection pooling |
| 4 | MEDIUM | `app.py:58-60` | Все ошибки Telegram → одинаковый `502`, нет дифференциации |
| 5 | LOW | `config.py:16-18` | Секреты с дефолтом `""` вместо required |
| 6 | LOW | `systemd:9` | ExecStart с `${VAR}` без fallback |
| 7 | LOW | `app.py:47` | Проверка `if payload.details` ловит и `None`, и `""` |
| 8 | LOW | `restart.sh` | Нет проверки pwd и post-restart healthcheck |
| 9 | LOW | `telegram.py:21` | `disable_web_page_preview` — deprecated в Telegram API 7.0+ |

**CRITICAL:** 0 | **HIGH:** 0 | **MEDIUM:** 4 | **LOW:** 5

---

## Общая оценка: 7.5 / 10

**Обоснование:**

Проект делает ровно то, что заявлено, и не больше — это сильная сторона. Код чистый, читаемый, модульная структура правильная (config / app / telegram разделены). Нет лишних абстракций, нет магии, нет скрытого состояния.

**Что хорошо:**
- Минимализм — 3 файла, ~95 строк Python, всё на виду.
- Правильные границы модулей — telegram.py не знает о конфигурации.
- `trust_env=False` — осознанная защита от утечки прокси-настроек хоста.
- `async with` для httpx — ресурсы не утекают.
- Логирование через `logger.exception()` — traceback сохраняется.

**Что не дотянуто:**
- Нет fail-fast при пустых секретах — сервис запускается, но не работает.
- httpx-клиент пересоздаётся, что неоптимально при нагрузке.
- Нет автотестов (ни одного).
- Нет дифференциации ошибок Telegram для клиента.

Для внутреннего alerting-сервиса на 5-10 rps — вполне продакшн-ready. Для масштабирования потребуется connection pooling и мониторинг.
