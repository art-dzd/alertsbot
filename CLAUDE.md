# Индекс документации alertsbot

Индексный файл проекта `alertsbot`.
Подробные регламенты и runbook вынесены в `docs/`.

## Кратко о проекте
- `alertsbot` принимает HTTP-уведомления от внутренних сервисов.
- Проверяет общий секрет в заголовке `X-Alerts-Token`.
- Пересылает сообщение в Telegram через Bot API.
- Не хранит состояние и не содержит бизнес-логику.

## Стек
- Python 3.11+, FastAPI, Uvicorn.
- `httpx` для исходящего запроса в Telegram API.
- `pydantic`/`pydantic-settings` для модели и конфигурации.
- `systemd`-юнит + `scripts/restart.sh` для эксплуатации.

## Принципы работы
1. Любой `POST /notify` сначала проходит авторизацию по `X-Alerts-Token`.
2. Конфигурация приходит только из `.env`/переменных окружения.
3. Сообщение формируется в предсказуемом текстовом формате.
4. Сбой Telegram не скрывается: логируется и отдаётся как `HTTP 502`.
5. Простота и прозрачность важнее усложнений (очередей/ретраев внутри сервиса нет).

## Каноническая документация
- `docs/architecture.md` — компоненты, границы системы, поток `/notify`.
- `docs/deploy.md` — запуск, systemd-деплой, post-check и rollback.
- `docs/testing.md` — текущий quality gate и smoke-сценарии.
- `docs/commands.md` — рабочие команды разработки и эксплуатации.
- `docs/design/notify-flow.md` — контракт запроса и инварианты форматирования.

## Ограничения
- Без валидного `ALERTS_TOKEN` отправка запрещена.
- Сервис не делает дедупликацию и повторные отправки.
- Изменение контракта `/notify` требует синхронного обновления документации.
- В проде нельзя запускать сервис без `.env` с заполненными секретами.

## Базовые команды
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn alertsbot.app:app --host 0.0.0.0 --port 9100
```

## Минимальный деплойный контур
- Подготовить/обновить `.env` на сервере.
- Выполнить `bash scripts/restart.sh` в каталоге проекта.
- Проверить `systemctl status alertsbot`, `healthz` и `journalctl`.

## SSH и прокси
- Для SSH используй короткие алиасы (`ssh aws`, `ssh macmini`, `ssh mts`, `ssh racknerd`, `ssh vpn`).
- Для внешних `ssh/scp/rsync` отключай прокси:
`env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY <command>`.
