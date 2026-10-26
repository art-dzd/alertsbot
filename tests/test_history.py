from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from pytest import LogCaptureFixture, MonkeyPatch

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
            [("2021-10-13T23:59:59.000+00:00",), ("2021-10-14T00:00:00.000+00:00",)],
        )
    store = history.History(path)
    store.cleanup()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT created_at FROM alerts").fetchall() == [
            ("2021-10-14T00:00:00.000+00:00",),
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


def test_schema_retries_only_after_sqlite_error(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    path = tmp_path / "history.db"
    store = history.History(str(path))
    assert store.insert({"message": "first"}, "test") is not None

    path.unlink()
    assert store.insert({"message": "second"}, "test") is None
    assert any(getattr(record, "event", None) == "history_error" for record in caplog.records)

    assert store.insert({"message": "third"}, "test") is not None
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM alerts").fetchone() == (1,)


def test_cleanup_failure_is_not_retried_before_interval(
    monkeypatch: MonkeyPatch, tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="alertsbot.history")
    now = 1000.0
    monkeypatch.setattr("alertsbot.history.time.monotonic", lambda: now)
    store = history.History(str(tmp_path / "missing" / "history.db"))

    store.cleanup()
    store.cleanup()

    errors = [
        record for record in caplog.records if getattr(record, "event", None) == "history_error"
    ]
    assert len(errors) == 1


def test_parallel_writes_do_not_lose_rows(tmp_path: Path, caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="alertsbot.history")
    path = tmp_path / "history.db"
    store = history.History(str(path))
    count = 32

    with ThreadPoolExecutor(max_workers=16) as pool:
        row_ids = list(
            pool.map(
                lambda index: store.insert({"message": f"message-{index}"}, "test"),
                range(count),
            )
        )

    assert all(row_id is not None for row_id in row_ids)
    assert not [
        record for record in caplog.records if getattr(record, "event", None) == "history_error"
    ]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM alerts").fetchone() == (count,)
