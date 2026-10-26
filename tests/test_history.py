from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from types import TracebackType
from typing import Any

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


def test_history_recovers_after_database_file_removed(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    store = history.History(str(path))
    assert store.insert({"message": "first"}, "test") is not None

    path.unlink()

    assert store.insert({"message": "second"}, "test") is not None


def test_history_recovers_after_database_file_truncated(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    store = history.History(str(path))
    assert store.insert({"message": "first"}, "test") is not None

    path.write_bytes(b"")

    assert store.insert({"message": "second"}, "test") is not None


class RecordingConnection:
    def __init__(self, connection: sqlite3.Connection, scripts: list[str]) -> None:
        self._connection = connection
        self._scripts = scripts

    def executescript(self, script: str) -> sqlite3.Cursor:
        self._scripts.append(script)
        return self._connection.executescript(script)

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, parameters)

    def __enter__(self) -> RecordingConnection:
        self._connection.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return self._connection.__exit__(exc_type, exc, tb)

    def close(self) -> None:
        self._connection.close()


def test_schema_is_created_once(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    real_connect = sqlite3.connect
    scripts: list[str] = []

    def connect(*args: Any, **kwargs: Any) -> RecordingConnection:
        return RecordingConnection(real_connect(*args, **kwargs), scripts)

    monkeypatch.setattr("alertsbot.history.sqlite3.connect", connect)
    store = history.History(str(tmp_path / "history.db"))

    assert store.insert({"message": "first"}, "test") is not None
    assert store.insert({"message": "second"}, "test") is not None
    assert len(scripts) == 1


def test_cleanup_time_advances_only_after_success(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    store = history.History(str(tmp_path / "history.db"))
    now = 1000.0
    monkeypatch.setattr("alertsbot.history.time.monotonic", lambda: now)
    calls = 0

    def fail(sql: str, parameters: tuple[Any, ...] = ()) -> None:
        nonlocal calls
        calls += 1
        return None

    def success(sql: str, parameters: tuple[Any, ...] = ()) -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(store, "_execute", fail)
    store.cleanup()
    assert calls == 1

    monkeypatch.setattr(store, "_execute", success)
    store.cleanup()
    assert calls == 2

    store.cleanup()
    assert calls == 2
