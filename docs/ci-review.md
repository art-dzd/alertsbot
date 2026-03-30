# CI/CD Review: alertsbot

Дата: 2026-03-20

---

## Текущее состояние

**CI/CD отсутствует полностью.**

| Артефакт | Наличие |
|---|---|
| `.github/workflows/` | Нет (удалены в коммите `47f4cb5 remove actions`) |
| `Makefile` | Нет |
| `docker-compose.yml` | Нет |
| `Dockerfile` | Нет |
| `pyproject.toml` | Нет |
| `tox.ini` | Нет |
| Pre-commit hooks | Нет |

---

## Текущий процесс деплоя

Ручной, в одно действие:

```
Разработчик → git pull на сервере → bash scripts/restart.sh → ручная проверка
```

### `scripts/restart.sh` — что делает

1. Создаёт `.venv` если не существует, обновляет pip
2. Устанавливает зависимости из `requirements.txt`
3. Копирует systemd-юнит в `/etc/systemd/system/`
4. `daemon-reload` → `enable` → `restart`

### Что **не** делает `restart.sh`

| Отсутствует | Риск |
|---|---|
| Проверка pwd (запуск из корня проекта) | Упадёт с непонятной ошибкой при запуске из другой директории |
| Lint / typecheck / compileall | Синтаксическая ошибка уедет в прод |
| Тесты | Сломанная логика уедет в прод |
| Post-restart healthcheck | Сервис мог не подняться — оператор не узнает без ручной проверки |
| Backup предыдущей версии | Откат — ручной `git checkout` |

### `systemd/alertsbot.service`

- `Restart=always`, `RestartSec=5` — при падении systemd перезапустит процесс. Хорошо.
- Нет `User=`/`Group=` — потенциально работает от root (см. security-review).
- Нет health check встроенного в systemd (`ExecStartPost=` с curl).

---

## Чего не хватает — матрица проверок

### Автоматические проверки при push/PR (CI)

| Проверка | Статус | Приоритет | Инструмент |
|---|---|---|---|
| Lint (стиль кода) | ❌ | Высокий | `ruff check` |
| Format (форматирование) | ❌ | Средний | `ruff format --check` |
| Type check | ❌ | Средний | `mypy --strict` |
| Тесты | ❌ | Высокий | `pytest` |
| Coverage gate | ❌ | Средний | `pytest-cov` с порогом ≥ 80% |
| Security scan зависимостей | ❌ | Высокий | `pip-audit` |
| Compile check | ❌ | Низкий | `python -m compileall` |
| Docker build | ❌ (нет Docker) | — | — |

### Автоматический деплой (CD)

| Проверка | Статус | Приоритет |
|---|---|---|
| Auto-deploy при merge в main | ❌ | Низкий (для 1 сервера — ручной деплой ок) |
| Blue-green / canary | ❌ | Низкий (один инстанс) |
| Post-deploy healthcheck | ❌ | Высокий |
| Rollback при failed healthcheck | ❌ | Средний |

### Docker

**Docker не используется.** Сервис запускается через systemd + venv напрямую. Для одного инстанса на одном сервере это адекватно. Docker добавит overhead без существенной пользы.

---

## Рекомендации

### Фаза 1 — Минимальный CI (GitHub Actions, ~1 час)

Файл `.github/workflows/ci.yml`:

```yaml
name: CI
on: [push, pull_request]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install ruff pytest pip-audit

      - name: Lint
        run: ruff check alertsbot/

      - name: Compile check
        run: python -m compileall alertsbot/

      - name: Security audit
        run: pip-audit -r requirements.txt

      # Раскомментировать когда появятся тесты:
      # - name: Tests
      #   run: pytest
```

**Что даёт:** автоматический lint + compile check + CVE-сканирование зависимостей на каждый push. Ловит 80% проблем, которые сейчас проходят незамеченными.

### Фаза 2 — Тесты и coverage gate (~2-3 часа)

После написания тестов (см. `test-gaps.md`) — добавить в CI:

```yaml
      - name: Tests with coverage
        run: pytest --cov=alertsbot --cov-fail-under=80
```

### Фаза 3 — Type checking (~30 мин)

```yaml
      - name: Type check
        run: |
          pip install mypy
          mypy alertsbot/ --strict
```

Потребует добавить type stubs или `# type: ignore` в нескольких местах. Для 95 строк — быстро.

### Фаза 4 — Post-deploy healthcheck в restart.sh

Добавить в конец `scripts/restart.sh`:

```bash
sleep 2
if curl -fsS http://127.0.0.1:9100/healthz > /dev/null 2>&1; then
  echo "✓ healthz OK"
else
  echo "✗ healthz FAILED — check journalctl -u alertsbot"
  exit 1
fi
```

### Фаза 5 — Pre-commit hooks (опционально)

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.0
    hooks:
      - id: ruff
      - id: ruff-format
```

Ловит ошибки до push — ещё быстрее, чем CI.

---

## Приоритетная дорожная карта

| # | Действие | Усилия | Эффект |
|---|---|---|---|
| 1 | GitHub Actions: ruff + compileall + pip-audit | 1 час | Автоматический lint + CVE scan на каждый push |
| 2 | Post-deploy healthcheck в restart.sh | 10 мин | Немедленная обратная связь при неудачном деплое |
| 3 | Добавить pytest в CI (после написания тестов) | 30 мин | Автоматическая проверка логики |
| 4 | Coverage gate ≥ 80% | 10 мин | Не даст упасть покрытию |
| 5 | mypy --strict | 30 мин | Статическая типизация |
| 6 | Pre-commit hooks (ruff) | 15 мин | Ловит ошибки до push |

**Итого:** ~3 часа работы превращают «деплой вслепую» в «деплой с автоматической страховкой на 4 уровнях» (lint → types → tests → healthcheck).
