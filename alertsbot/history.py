"""История уведомлений; отказ хранилища не останавливает доставку."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any

from alertsbot.logging import exception_fields, utc_now

logger = logging.getLogger(__name__)
# Алерты редкие и лёгкие (~70 МБ/год при 100 в день), а повторы и тренды ищем на годы назад.
RETENTION_DAYS = 1826  # 5 лет
CLEANUP_INTERVAL_SECONDS = 3600
SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    service TEXT,
    title TEXT,
    message TEXT,
    details TEXT,
    event_id TEXT,
    client TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    telegram_status INTEGER,
    reason TEXT,
    sent_at TEXT,
    cached INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_created_at ON alerts(created_at);
"""


class History:
    def __init__(self, path: str) -> None:
        self.path = path
        self._last_cleanup = float("-inf")
        self._schema_ready = False
        self._db_lock = threading.Lock()

    def _execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> int | None:
        # lean: один доступ к SQLite за раз для ~15 отправителей, без очереди и потока.
        try:
            with self._db_lock:
                with closing(sqlite3.connect(self.path, timeout=0.1)) as connection, connection:
                    if not self._schema_ready:
                        connection.executescript(SCHEMA)
                        self._schema_ready = True
                    cursor = connection.execute(sql, parameters)
                    return cursor.lastrowid or 0
        except (sqlite3.Error, OSError, UnicodeError) as error:
            if isinstance(error, sqlite3.Error):
                self._schema_ready = False
            logger.error(
                "История алертов недоступна",
                extra={
                    "event": "history_error",
                    "fields": {
                        "reason": "database_failure",
                        **exception_fields(error),
                    },
                },
            )
            return None

    def cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < CLEANUP_INTERVAL_SECONDS:
            return
        self._last_cleanup = now
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(
            timespec="milliseconds",
        )
        self._execute("DELETE FROM alerts WHERE created_at < ?", (cutoff,))

    def insert(self, payload: dict[str, Any], client: str, status: str = "pending") -> int | None:
        self.cleanup()
        return self._execute(
            """INSERT INTO alerts
            (created_at, service, title, message, details, event_id, client, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                utc_now(),
                payload.get("service"),
                payload.get("title"),
                payload.get("message"),
                payload.get("details"),
                payload.get("event_id"),
                client,
                status,
            ),
        )

    def finish(
        self,
        row_id: int | None,
        status: str,
        http_status: int,
        telegram_status: int | None,
        reason: str,
        sent_at: str | None = None,
        cached: bool = False,
    ) -> bool:
        if row_id is None:
            return False
        return (
            self._execute(
                """UPDATE alerts SET status=?, http_status=?, telegram_status=?, reason=?,
            sent_at=?, cached=? WHERE id=?""",
                (status, http_status, telegram_status, reason, sent_at, cached, row_id),
            )
            is not None
        )

    def ready(self) -> bool:
        return self._execute("SELECT id FROM alerts LIMIT 0") is not None
