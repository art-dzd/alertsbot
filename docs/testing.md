# Тестирование alertsbot

## Текущее состояние
В репозитории есть unit-тесты FastAPI-слоя и Telegram-клиента.
Quality gate: `pytest`, `ruff`, `mypy`.

## Локальные проверки перед деплоем
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest -q --tb=short
ruff check .
mypy alertsbot tests
```

Проверка, что приложение поднимается:
```bash
uvicorn alertsbot.app:app --host 127.0.0.1 --port 9100
curl -fsS http://127.0.0.1:9100/healthz
```

## Smoke-тест `/notify`
Негативный кейс (неверный токен, ожидаем `401`):
```bash
curl -i -X POST http://127.0.0.1:9100/notify \
  -H 'Content-Type: application/json' \
  -H 'X-Alerts-Token: wrong-token' \
  -d '{"service":"demo","title":"t","message":"m"}'
```

Позитивный кейс (валидный токен, ожидаем `200` и `{"status":"sent"}`):
```bash
curl -i -X POST http://127.0.0.1:9100/notify \
  -H 'Content-Type: application/json' \
  -H "X-Alerts-Token: ${ALERTS_TOKEN}" \
  -d '{"service":"demo","title":"t","message":"m","details":"ok"}'
```

## Критерии готовности к релизу
- Приложение стартует без traceback.
- `/healthz` отвечает успешно.
- `/notify` корректно разделяет `200`, `401`, `422`, `424`, `502`.
- Telegram `429` ретраится один раз с ограниченной паузой.
- Telegram `400` не ретраится и маппится в `422`.
- Telegram `401/403/404` не ретраятся и маппятся в `424`.
- В `journalctl` нет новых необработанных исключений.

## Долг по качеству
- Добавить идемпотентность по `event_id` с TTL.
- Добавить post-restart healthcheck в `scripts/restart.sh`.
