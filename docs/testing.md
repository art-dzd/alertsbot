# Тестирование alertsbot

## Текущее состояние
Автоматические unit/integration тесты в репозитории отсутствуют.
Минимальный quality gate сейчас строится на проверке импорта, запуска и smoke-тестах API.

## Локальные проверки перед деплоем
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m compileall alertsbot
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
- `/notify` корректно разделяет `401` и `200/502` сценарии.
- В `journalctl` нет новых необработанных исключений.

## Долг по качеству
Рекомендуется добавить автотесты на:
- авторизацию заголовка `X-Alerts-Token`;
- форматирование текста уведомления;
- маппинг ошибок Telegram в `HTTP 502`.
