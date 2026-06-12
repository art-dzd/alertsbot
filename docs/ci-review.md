# CI/CD: alertsbot

Дата актуализации: 2026-06-12.
Исторический обзор от 2026-03-20 («CI/CD отсутствует полностью») устарел:
CI/CD добавлен 2026-05-28 (коммит `3a7c2e6`).

## Текущее состояние

CI/CD есть: Gitea Actions, `.gitea/workflows/ci-deploy.yml`,
триггеры — push в `main` и `workflow_dispatch`.

- Runner: `act_runner` на прод-хосте `mts` (systemd-юнит `gitea-runner-mts.service`),
  метка `deploy-mts`. Шаги job получают pty — в sudo-логе видны как `TTY=pts/0`.
- Job `ci`: `ruff check` + `pytest` в отдельном CI-venv (`/home/ai/.cache/alertsbot-ci-venv`).
- Job `deploy`: после зелёного `ci`, под flock — `git reset --hard` на SHA пуша,
  `pip install`, установка systemd-юнита, `daemon-reload`, `restart`,
  healthcheck (до 10 попыток), автоматический откат на предыдущий SHA при сбое.
- `.github/workflows.disabled/` — отключённые старые GitHub-workflow, не используются.

## Чего в CI нет

| Проверка | Статус |
|---|---|
| `mypy --strict` | в локальном quality gate, в CI не запускается |
| `pip-audit` | нет |
| Coverage gate | нет |
| Canary / blue-green | нет — один инстанс, осознанно |

## Ручной путь

`scripts/restart.sh` — резерв на случай недоступности CI: venv + зависимости,
копирование юнита, `daemon-reload`/`enable`/`restart`. Без тестов, healthcheck
и отката — после ручного запуска проверять прод по `docs/deploy.md`.
