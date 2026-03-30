# Аудит зависимостей: alertsbot

Дата: 2026-03-20

---

## Прямые зависимости (requirements.txt)

| Пакет | В проекте | Последняя стабильная | Отставание | Дата релиза в проекте |
|---|---|---|---|---|
| fastapi | 0.115.5 | 0.135.1 | 20 minor | ноябрь 2024 |
| uvicorn[standard] | 0.32.0 | 0.42.0 | 10 minor | октябрь 2024 |
| httpx | 0.27.2 | 0.28.1 | 1 minor | август 2024 |
| pydantic | 2.10.4 | 2.12.5 | 2 minor | декабрь 2024 |
| pydantic-settings | 2.6.1 | 2.13.1 | 7 minor | ноябрь 2024 |

Все зависимости датированы осенью 2024 — **~1.5 года без обновлений**.

---

## 1. Пиннинг версий

**Статус: ОК** — все 5 зависимостей пиннены до точных версий (`==`).

| Пакет | Формат | Оценка |
|---|---|---|
| `fastapi==0.115.5` | exact pin | ✓ |
| `uvicorn[standard]==0.32.0` | exact pin + extras | ✓ |
| `httpx==0.27.2` | exact pin | ✓ |
| `pydantic==2.10.4` | exact pin | ✓ |
| `pydantic-settings==2.6.1` | exact pin | ✓ |

**Замечание:** Нет `requirements.lock` или `pip freeze` для фиксации транзитивных зависимостей. Пиннены только прямые, а транзитивные (h11, starlette, anyio, httpcore) плывут. Это источник CVE-проблем — см. секцию 5.

---

## 2. Устаревшие зависимости

### fastapi 0.115.5 → 0.135.1 (20 minor, HIGH)

Самое большое отставание. Значимые изменения:
- **0.116+**: обновление пиннинга starlette (0.41 → 0.52+), закрывающее CVE в starlette.
- **0.130.0**: JSON-сериализация переведена на Pydantic/Rust.
- **0.132.0**: `strict_content_type` для `JSONResponse` — потенциальный breaking change.

**Риск для alertsbot:** Низкий — сервис отдаёт простые `dict`, не использует `ORJSONResponse`. Обновление должно пройти гладко.

### uvicorn 0.32.0 → 0.42.0 (10 minor, HIGH)

**Ключевое:** версии 0.34+ тянут `h11>=0.16.0`, закрывающий CVE-2025-43859.
Breaking changes минимальны — рефакторинги и улучшения совместимости.

### httpx 0.27.2 → 0.28.1 (1 minor, LOW)

- **0.28.0**: убраны deprecated параметры (`app=`), изменено поведение `trust_env`.
- **Для alertsbot:** в `telegram.py:23` уже используется `trust_env=False` — совместимо. Параметр `app=` не используется.

### pydantic 2.10.4 → 2.12.5 (2 minor, LOW)

Минорные улучшения. Нет серьёзных breaking changes для базового использования (`BaseModel`, `Field`).

### pydantic-settings 2.6.1 → 2.13.1 (7 minor, MEDIUM)

Добавлены новые источники конфигурации (AWS Secrets Manager, Azure Key Vault). API ядра (`BaseSettings`, `SettingsConfigDict`) стабилен. Breaking changes для текущего паттерна нет.

---

## 3. Использование зависимостей в коде

| Пакет | Где импортируется | Что используется | Нужен? |
|---|---|---|---|
| fastapi | `app.py:7` | `FastAPI`, `Header`, `HTTPException` | Да |
| pydantic | `app.py:8` | `BaseModel`, `Field` | Да |
| pydantic-settings | `config.py:8` | `BaseSettings`, `SettingsConfigDict` | Да |
| httpx | `telegram.py:5` | `httpx.AsyncClient` | Да |
| uvicorn | ExecStart в systemd | ASGI-сервер | Да (runtime) |

**Неиспользуемых зависимостей: 0.** Каждый пакет реально импортируется и применяется. Минимальный набор — ничего лишнего.

---

## 4. Дублирование функционала

**Дублирования нет.** Каждая зависимость закрывает уникальную роль:

```
fastapi          → HTTP-фреймворк (роутинг, валидация, OpenAPI)
uvicorn          → ASGI-сервер (запуск приложения)
httpx            → HTTP-клиент (исходящие запросы к Telegram)
pydantic         → Валидация данных и моделей
pydantic-settings → Загрузка конфигурации из env/files
```

Связи: `fastapi` зависит от `pydantic` и `starlette`; `pydantic-settings` зависит от `pydantic`. Это штатные зависимости, не дублирование.

---

## 5. Известные CVE

### Прямые зависимости

| Пакет | Версия | CVE | Статус |
|---|---|---|---|
| fastapi | 0.115.5 | Нет | Чисто |
| uvicorn | 0.32.0 | Нет | Чисто |
| httpx | 0.27.2 | Нет | Чисто |
| pydantic | 2.10.4 | Нет (CVE-2024-3772 закрыта в 2.4.0) | Чисто |
| pydantic-settings | 2.6.1 | Нет | Чисто |

### Транзитивные зависимости — 3 CVE

| Пакет | Версия (через) | CVE | CVSS | Описание | Фикс |
|---|---|---|---|---|---|
| **h11** | 0.14.x (uvicorn) | **CVE-2025-43859** | **9.1 CRITICAL** | HTTP Request Smuggling через некорректный парсинг chunked-encoding | h11 ≥ 0.16.0 → uvicorn ≥ 0.34 |
| **starlette** | ~0.41.x (fastapi) | **CVE-2025-62727** | HIGH | Уязвимость в обработке запросов | starlette ≥ 0.49.1 → fastapi ≥ 0.124 |
| **starlette** | ~0.41.x (fastapi) | **CVE-2025-54121** | 5.3 MEDIUM | DoS при парсинге multipart с большими файлами | starlette ≥ 0.47.2 → fastapi ≥ 0.116 |

**CVE-2024-47874** (starlette, multipart DoS) — **уже закрыта**: fastapi 0.115.5 требует starlette ≥ 0.40.0.

---

## Сводка рисков

| Критерий | Оценка | Комментарий |
|---|---|---|
| Пиннинг прямых | ✅ Отлично | Все 5 на exact pin |
| Пиннинг транзитивных | ❌ Нет | Нет lock-файла, транзитивные плывут |
| Устаревшие версии | ⚠️ Средне | 1.5 года без обновлений, до 20 minor за fastapi |
| Неиспользуемые | ✅ Отлично | 0 лишних, каждая нужна |
| Дублирование | ✅ Отлично | 0, роли не пересекаются |
| CVE | ❌ Критично | 1 CRITICAL + 1 HIGH + 1 MEDIUM в транзитивных |
| Автоматический аудит | ❌ Нет | Нет pip-audit, Dependabot, Snyk |

---

## Рекомендации по приоритету

### 1. [CRITICAL] Обновить uvicorn до ≥ 0.34 — закрыть CVE-2025-43859

```
uvicorn[standard]==0.42.0
```

Закрывает h11 CRITICAL CVE. Минимальные breaking changes. Максимальный ROI.

### 2. [HIGH] Обновить fastapi до ≥ 0.124 — закрыть CVE starlette

```
fastapi==0.135.1
```

Закрывает CVE-2025-62727 (HIGH) и CVE-2025-54121 (MEDIUM) в starlette. Проверить поведение `JSONResponse` после обновления.

### 3. [MEDIUM] Обновить остальные до актуальных

```
httpx==0.28.1
pydantic==2.12.5
pydantic-settings==2.13.1
```

Нет CVE, но уменьшает tech debt. httpx 0.28 — проверить совместимость `trust_env=False` (должно работать).

### 4. [MEDIUM] Зафиксировать транзитивные зависимости

Добавить `pip freeze > requirements.lock` после установки и использовать его для деплоя:
```bash
pip install -r requirements.lock
```

Это гарантирует, что prod получит те же транзитивные версии, что и dev.

### 5. [LOW] Добавить автоматический аудит зависимостей

Добавить `pip-audit` в dev-зависимости и в CI (когда появится):
```bash
pip install pip-audit
pip-audit -r requirements.txt
```

Или подключить GitHub Dependabot / Snyk для автоматических alerts.
