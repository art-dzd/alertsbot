# Security Review: alertsbot

Аудит безопасности всего кода проекта. Дата: 2026-03-20.

---

## Находки

### HIGH | `app.py:43` | Timing attack на сравнение токена

```python
if not settings.alerts_token or x_alerts_token != settings.alerts_token:
```

Оператор `!=` для строк сравнивает посимвольно и выходит при первом различии. Время ответа коррелирует с количеством совпавших символов — атакующий может подбирать токен по одному символу, замеряя latency.

**Практичность:** Для эксплуатации нужна сетевая среда с низким jitter и тысячи запросов. Для внутреннего сервиса в LAN — маловероятно, но для публичного — реальный вектор.

**Рекомендация:** Заменить на `hmac.compare_digest(x_alerts_token or "", settings.alerts_token)` — constant-time сравнение из стандартной библиотеки.

---

### HIGH | `requirements.txt:2` | CVE-2025-43859 в транзитивной зависимости h11 (через uvicorn)

`uvicorn[standard]==0.32.0` тянет `h11 < 0.16.0`. CVE-2025-43859 (CVSS 9.1, CRITICAL) — некорректная обработка chunked-encoding позволяет HTTP Request Smuggling при наличии уязвимого reverse-proxy перед сервисом.

**Практичность:** Эксплуатация требует уязвимого прокси (nginx/haproxy определённых версий) перед alertsbot. Если сервис слушает напрямую без прокси — риск значительно ниже.

**Рекомендация:** Обновить uvicorn до версии, которая тянет `h11>=0.16.0`, или явно пинить `h11>=0.16.0` в `requirements.txt`.

---

### MEDIUM | `systemd/alertsbot.service` | Сервис запускается без явного User=/Group=

```ini
[Service]
Type=simple
WorkingDirectory=/alertsbot
```

В юните нет `User=` и `Group=`. Если `restart.sh` запускается от root (а он использует `sudo`), сервис будет работать от root. Процесс uvicorn, слушающий на сетевом порту от root — расширение поверхности атаки: RCE-уязвимость в любой зависимости даст полный контроль над хостом.

**Рекомендация:** Добавить `User=alertsbot` и `Group=alertsbot` (или другой непривилегированный пользователь) в секцию `[Service]`.

---

### MEDIUM | `app.py:23` | OpenAPI-документация доступна без авторизации

```python
app = FastAPI(title="alertsbot")
```

FastAPI по умолчанию генерирует:
- `GET /docs` — Swagger UI
- `GET /redoc` — ReDoc
- `GET /openapi.json` — OpenAPI-схема

Все доступны без авторизации. Раскрывают: структуру API, модель `NotifyRequest`, имя заголовка авторизации. Для внутреннего сервиса — информационная утечка, упрощающая разведку.

**Рекомендация:** Отключить в проде: `FastAPI(title="alertsbot", docs_url=None, redoc_url=None, openapi_url=None)`.

---

### MEDIUM | `config.py:16-18` | Секреты имеют default="" — нет fail-fast

```python
alerts_bot_token: str = Field(default="", alias="ALERTS_BOT_TOKEN")
alerts_chat_id: str = Field(default="", alias="ALERTS_CHAT_ID")
alerts_token: str = Field(default="", alias="ALERTS_TOKEN")
```

Сервис стартует с пустыми секретами. `healthz` отвечает OK. Оператор может не заметить, что alerting не работает — silent failure для сервиса, чья задача — не молчать о проблемах.

**Рекомендация:** Убрать `default=""` у трёх секретов. Без `.env` pydantic выбросит `ValidationError` при старте.

---

### MEDIUM | `app.py:17-20` | Нет ограничений на длину входных строк

```python
service: str = Field(..., description="Название сервиса")
title: str = Field(..., description="Заголовок уведомления")
message: str = Field(..., description="Основной текст уведомления")
details: str | None = Field(default=None, description="Дополнительные детали")
```

Ни одно поле не ограничено по длине (`max_length`). Атакующий с валидным токеном может отправить payload в десятки мегабайт. Последствия:
- Память uvicorn-процесса раздувается.
- Telegram API отклонит сообщение >4096 символов — бесполезный трафик.
- Многократные запросы → DoS.

**Рекомендация:** Добавить `max_length` к полям. Разумные лимиты: `service` — 128, `title` — 256, `message` — 4000, `details` — 4000 (Telegram API лимит — 4096 на всё сообщение).

---

### MEDIUM | Нет rate limiting

Ни в коде, ни в конфигурации нет ограничения частоты запросов. Клиент с валидным токеном (или без — получит 401, но нагрузка будет) может флудить сервис.

**Рекомендация:** Добавить rate limiting на уровне reverse-proxy (если есть) или через `slowapi` / middleware в FastAPI. Для alerting-сервиса разумный лимит — 10-30 req/s.

---

### LOW | `telegram.py:17` | Bot-token попадает в URL — может утечь в логи

```python
url = f"https://api.telegram.org/bot{token}/sendMessage"
```

Telegram bot token — полноценный credential. Он вшивается в URL-path, что означает:
- При ошибке httpx может залогировать URL с токеном.
- Если перед сервисом стоит прокси с access-логами — токен пишется в каждой строке.

**Практичность:** httpx по умолчанию не логирует URL на уровне WARNING+. Но при `DEBUG`-логировании или при использовании прокси — утечка реальна.

**Рекомендация:** Не критично для текущего setup, но при добавлении DEBUG-логирования или прокси перед Telegram — учитывать. Альтернатива: передавать token через заголовок (Telegram API не поддерживает это нативно, так что тут варианта нет).

---

### LOW | `app.py:46-48` | Потенциальный log injection через пользовательский ввод

```python
text = f"{payload.service}\n{payload.title}\n{payload.message}"
```

Поля `service`, `title`, `message` — пользовательский ввод без санитизации. Они не попадают в `logger.*()` напрямую (логируется только `"Failed to send Telegram message"` + traceback). Однако если в будущем добавится логирование содержимого запроса, символы `\n` в полях позволят инжектить фейковые строки в лог.

**Рекомендация:** Не требует немедленных действий. При добавлении логирования payload — экранировать newlines.

---

### LOW | `app.py` | Нет HTTPS на уровне приложения

Uvicorn запускается без `--ssl-*` параметров. Трафик между клиентами и alertsbot идёт по HTTP. Заголовок `X-Alerts-Token` передаётся открытым текстом.

**Практичность:** Для внутреннего сервиса в trusted LAN — приемлемо. Если трафик выходит за пределы LAN — токен перехватывается.

**Рекомендация:** Терминировать TLS на reverse-proxy перед сервисом или добавить `--ssl-keyfile/--ssl-certfile` в uvicorn.

---

### INFO | Общие положительные моменты (не требуют действий)

| Что | Где | Почему хорошо |
|---|---|---|
| `trust_env=False` | `telegram.py:23` | httpx не подхватывает прокси из окружения хоста — защита от случайной утечки трафика через чужой прокси |
| `.env` в `.gitignore` | `.gitignore:1` | Секреты не попадут в git |
| Нет hardcoded секретов | весь код | Grep по `token=`, `password=`, `secret=`, `key=` — чисто |
| CSRF-защита через custom header | `app.py:39` | Заголовок `X-Alerts-Token` нельзя отправить кросс-доменным `<form>` — CSRF невозможен |
| Нет SQL/NoSQL | — | Инъекции в БД невозможны — БД нет |
| Нет файловых операций | — | Path traversal невозможен |
| Нет HTML-рендеринга | — | XSS невозможен |
| Нет JWT/криптографии | — | Нечему быть неправильным |
| `extra="ignore"` в Settings | `config.py:14` | Лишние env-переменные не вызывают ошибку — нет утечки через error messages |

---

## OWASP Top 10 — чеклист

| # | Категория | Применимость | Статус |
|---|---|---|---|
| A01 | Broken Access Control | Есть | `X-Alerts-Token` проверяется; `/healthz` открыт намеренно; `/docs` открыт непреднамеренно |
| A02 | Cryptographic Failures | Частично | Timing attack на сравнение токена; нет HTTPS на уровне app |
| A03 | Injection | Нет | Нет SQL/HTML/shell — пользовательский ввод уходит как plaintext в Telegram |
| A04 | Insecure Design | Нет | Дизайн адекватен scope |
| A05 | Security Misconfiguration | Частично | OpenAPI docs доступны; systemd без User=; пустые дефолты секретов |
| A06 | Vulnerable Components | Да | CVE-2025-43859 в h11 через uvicorn |
| A07 | Auth Failures | Минимально | Timing attack; нет brute-force защиты |
| A08 | Data Integrity Failures | Нет | Нет десериализации/CI-CD |
| A09 | Logging Failures | Минимально | Bot-token в URL может утечь при DEBUG |
| A10 | SSRF | Нет | URL Telegram API захардкожен; пользовательский ввод не влияет на URL |

---

## Сводная таблица

| # | Severity | Файл:строка | Проблема | Рекомендация |
|---|----------|-------------|----------|--------------|
| 1 | HIGH | `app.py:43` | Timing attack на сравнение токена | `hmac.compare_digest()` |
| 2 | HIGH | `requirements.txt:2` | CVE-2025-43859 в h11 (через uvicorn) | Обновить uvicorn или пинить h11>=0.16.0 |
| 3 | MEDIUM | `systemd/alertsbot.service` | Нет `User=`/`Group=` — может работать от root | Добавить непривилегированного пользователя |
| 4 | MEDIUM | `app.py:23` | OpenAPI docs доступны без авторизации | `docs_url=None, redoc_url=None, openapi_url=None` |
| 5 | MEDIUM | `config.py:16-18` | Секреты с default="" — нет fail-fast | Убрать дефолты для секретов |
| 6 | MEDIUM | `app.py:17-20` | Нет max_length на входных полях | Добавить лимиты (4000 символов) |
| 7 | MEDIUM | — | Нет rate limiting | slowapi или ограничение на reverse-proxy |
| 8 | LOW | `telegram.py:17` | Bot-token в URL — может утечь в логи | Учитывать при DEBUG-логировании |
| 9 | LOW | `app.py:46-48` | Потенциальный log injection | Экранировать newlines при логировании payload |
| 10 | LOW | `app.py` | Нет HTTPS | TLS на reverse-proxy |

**CRITICAL:** 0 | **HIGH:** 2 | **MEDIUM:** 5 | **LOW:** 3
