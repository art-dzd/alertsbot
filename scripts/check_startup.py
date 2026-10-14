"""Проверяет JSON-журнал нового запуска; вызывается из CI после healthcheck."""

import json
import sys

version = sys.argv[1]
startup_seen = False
for line in sys.stdin:
    try:
        record = json.loads(line)
    except ValueError:
        sys.exit("Deploy failed: invalid JSON service log")
    if not isinstance(record, dict):
        sys.exit("Deploy failed: invalid service log record")
    if record.get("level") in {"ERROR", "CRITICAL"}:
        sys.exit("Deploy failed: ERROR in new invocation")
    if record.get("event") == "startup" and record.get("version") == version:
        startup_seen = True
        # Дальше рабочий трафик: ERROR доставки клиента не повод откатывать выпуск.
        break
if not startup_seen:
    sys.exit("Deploy failed: startup with target version is missing")
print(f"Startup verified: {version}, no ERROR")
