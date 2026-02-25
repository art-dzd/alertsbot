# Команды alertsbot

## Подготовка окружения
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

## Локальный запуск
```bash
uvicorn alertsbot.app:app --host 0.0.0.0 --port 9100
```

## Проверка health
```bash
curl -fsS http://127.0.0.1:9100/healthz
```

## Отправка тестового уведомления
```bash
curl -X POST http://127.0.0.1:9100/notify \
  -H 'Content-Type: application/json' \
  -H "X-Alerts-Token: ${ALERTS_TOKEN}" \
  -d '{"service":"alertsbot","title":"test","message":"hello"}'
```

## Перезапуск сервиса на сервере
```bash
bash scripts/restart.sh
```

## Диагностика systemd
```bash
systemctl status alertsbot --no-pager
journalctl -u alertsbot -n 200 --no-pager
systemctl restart alertsbot
```

## Быстрая проверка синтаксиса Python
```bash
python -m compileall alertsbot
```
