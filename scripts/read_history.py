#!/usr/bin/python3
"""Читает историю alertsbot для пересыльщика с web."""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from contextlib import closing

# Монитор infra (telegram-proxy-monitor) раз в минуту проверяет прокси, которыми alertsbot ходит
# в Telegram. Его алерт идёт мимо истории, поэтому вкладка узнаёт о сбое отсюда.
TELEGRAM_ROUTE_FILE = '/var/lib/node_exporter/textfile_collector/telegram_proxy.prom'
TELEGRAM_ROUTE_METRIC = 'telegram_route_up{route="alertsbot"} '


def telegram_route():
    try:
        with open(TELEGRAM_ROUTE_FILE, encoding='utf-8') as metrics:
            age = time.time() - os.fstat(metrics.fileno()).st_mtime
            value = next(
                (line.split()[-1] for line in metrics if line.startswith(TELEGRAM_ROUTE_METRIC)),
                None,
            )
    except OSError:
        return None
    return {'value': value, 'age_seconds': round(age)}


try:
    urllib.request.urlopen('http://127.0.0.1:9100/readyz', timeout=12).close()
except Exception:
    sys.exit(3)

with closing(sqlite3.connect(
    'file:/var/lib/alertsbot/alerts.db?mode=ro', uri=True, timeout=5
)) as db:
    db.row_factory = sqlite3.Row
    maximum = db.execute('SELECT COALESCE(MAX(id), 0) FROM alerts').fetchone()[0]
    rows = []
    reset = False
    if sys.argv[1] != 'init':
        cursor = int(sys.argv[1])
        reset = cursor > maximum
        rows = [dict(row) for row in db.execute(
            'SELECT id, created_at, service, title, message, status, http_status, reason, cached '
            'FROM alerts WHERE id > ? ORDER BY id LIMIT ?',
            (0 if reset else cursor, 100),
        )]
    print(json.dumps(
        {'max_id': maximum, 'rows': rows, 'reset': reset, 'telegram_route': telegram_route()},
        ensure_ascii=True,
    ))
