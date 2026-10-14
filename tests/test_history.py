from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from pytest import MonkeyPatch

from alertsbot import history


def test_retention_at_startup_and_at_most_hourly(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    now = 1000.0

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Clock:
            return cls(2026, 10, 14, tzinfo=timezone.utc)

    monkeypatch.setattr(history, "datetime", Clock)
    monkeypatch.setattr("alertsbot.history.time.monotonic", lambda: now)
    path = str(tmp_path / "history.db")
    store = history.History(path)
    store.cleanup()

    with closing(sqlite3.connect(path)) as db, db:
        db.executemany(
            "INSERT INTO alerts(created_at, client, status) VALUES (?, 'test', 'sent')",
            [("2026-07-15T23:59:59.000+00:00",), ("2026-07-16T00:00:00.000+00:00",)],
        )
    store = history.History(path)
    store.cleanup()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT created_at FROM alerts").fetchall() == [
            ("2026-07-16T00:00:00.000+00:00",),
        ]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO alerts(created_at, client, status) VALUES ('2000', 'test', 'sent')")
    now += 3599
    store.insert({"message": "new"}, "test")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM alerts WHERE created_at='2000'").fetchone() == (1,)
    now += 1
    store.insert({"message": "new"}, "test")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM alerts WHERE created_at='2000'").fetchone() == (0,)


def test_history_recovers_after_database_file_removed(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    store = history.History(str(path))
    assert store.insert({"message": "first"}, "test") is not None

    path.unlink()

    assert store.insert({"message": "second"}, "test") is not None
