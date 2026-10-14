#!/usr/bin/python3
"""Пересылает новые строки истории alertsbot во вкладку tpanel; только stdlib."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, cast
from uuid import uuid4

BATCH_LIMIT = 100
PENDING_MAX_AGE = timedelta(minutes=5)
MESSAGE_PREVIEW_LIMIT = 300
MESSAGE_LIMIT = 6000
MOSCOW = timezone(timedelta(hours=3))
VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
REMOTE_QUERY = f"""
import json
import sqlite3
import sys
from contextlib import closing

with closing(sqlite3.connect(
    'file:/var/lib/alertsbot/alerts.db?mode=ro', uri=True, timeout=5
)) as db:
    db.row_factory = sqlite3.Row
    maximum = 0
    rows = []
    if sys.argv[1] == 'init':
        maximum = db.execute('SELECT COALESCE(MAX(id), 0) FROM alerts').fetchone()[0]
    else:
        rows = [dict(row) for row in db.execute(
            'SELECT id, created_at, service, title, message, status, http_status, reason, cached '
            'FROM alerts WHERE id > ? ORDER BY id LIMIT ?',
            (int(sys.argv[1]), {BATCH_LIMIT})
        )]
    print(json.dumps({{'max_id': maximum, 'rows': rows}}, ensure_ascii=True))
"""


class Alert(TypedDict):
    id: int
    created_at: str
    service: str | None
    title: str | None
    message: str | None
    status: str
    http_status: int | None
    reason: str | None
    cached: int


def parse_time(value: str) -> datetime:
    # Python 3.10 не понимает суффикс Z в fromisoformat.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid_timestamp")
    return parsed


def fetch_history(last_id: int | None) -> tuple[int, list[Alert]]:
    cursor = "init" if last_id is None else str(last_id)
    command = f"sudo -n -u alertsbot python3 -c {shlex.quote(REMOTE_QUERY)} {cursor}"
    response = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mts", command],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        timeout=25,
    )
    data = json.loads(response.stdout)
    if not isinstance(data, dict) or type(data.get("max_id")) is not int or data["max_id"] < 0:
        raise ValueError("invalid_history")
    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) > BATCH_LIMIT:
        raise ValueError("invalid_history")
    previous = last_id or 0
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= previous:
            raise ValueError("invalid_history")
        if row.get("status") not in {"pending", "sent", "failed", "rejected"}:
            raise ValueError("invalid_history")
        if type(row.get("cached")) is not int or row["cached"] not in {0, 1}:
            raise ValueError("invalid_history")
        for field in ("service", "title", "message", "reason", "http_status"):
            value = row[field]
            expected = int if field == "http_status" else str
            if value is not None and not isinstance(value, expected):
                raise ValueError("invalid_history")
        if not isinstance(row.get("created_at"), str):
            raise ValueError("invalid_history")
        parse_time(row["created_at"])
        previous = row["id"]
    return data["max_id"], cast(list[Alert], rows)


def compact(value: str | None, limit: int) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def format_message(rows: list[Alert], id_from: int, id_to: int) -> str:
    text = f"Алерты: {len(rows)} · история id {id_from}–{id_to}"
    for index, row in enumerate(rows):
        timestamp = parse_time(row["created_at"]).astimezone(MOSCOW).strftime("%d.%m %H:%M")
        status = row["status"]
        if status == "failed":
            status += f" {row['http_status']}" if row["http_status"] is not None else ""
        elif status == "pending":
            status += " (итог неизвестен)"
        reason = compact(row["reason"], 256)
        block = (
            f"\n#{row['id']} · {timestamp} МСК · {compact(row['service'], 128) or '—'} · "
            f"{compact(row['title'], 256) or '—'} · {status}"
            + (f" · {reason}" if reason else "")
            + f"\n{compact(row['message'], MESSAGE_PREVIEW_LIMIT)}"
        )
        remaining = rows[index + 1 :]
        suffix = (
            f"\nещё {len(remaining)}, id {remaining[0]['id']}–{remaining[-1]['id']}"
            if remaining
            else ""
        )
        if len(text) + len(block) + len(suffix) > MESSAGE_LIMIT:
            return text + f"\nещё {len(rows) - index}, id {row['id']}–{rows[-1]['id']}"
        text += block
    return text


def save_state(path: Path, last_id: int) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        try:
            temporary.write(f"{last_id}\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(temporary.name, path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)


def log_result(started: float, work_id: str, **fields: object) -> None:
    caller = traceback.extract_stack(limit=2)[0]
    print(
        json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "level": "ERROR" if fields.get("outcome") == "error" else "INFO",
                "service": "alerts-tab-forwarder",
                "version": VERSION,
                "event": "forward_done",
                "message": "Проход пересыльщика завершён",
                "code": f"tab_forwarder.py:{caller.lineno}",
                "request_id": work_id,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                **fields,
            },
            ensure_ascii=True,
        )
    )


def main() -> int:
    started = time.monotonic()
    work_id = uuid4().hex
    stage = "state"
    stage_started = started
    try:
        state = Path(os.environ["STATE_DIRECTORY"]) / "last_id"
        try:
            last_id = int(state.read_text(encoding="ascii").strip())
        except FileNotFoundError:
            last_id = None
        if last_id is not None and last_id < 0:
            raise ValueError("invalid_cursor")

        stage = "ssh"
        stage_started = time.monotonic()
        maximum, rows = fetch_history(last_id)
        ssh_ms = round((time.monotonic() - stage_started) * 1000, 3)
        if last_id is None:
            stage = "state"
            stage_started = time.monotonic()
            save_state(state, maximum)
            log_result(
                started,
                work_id,
                outcome="ok",
                count=0,
                id_from=maximum,
                id_to=maximum,
                reason="initialized",
                ssh_ms=ssh_ms,
            )
            return 0

        ready: list[Alert] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            if row["status"] == "pending" and now - parse_time(row["created_at"]) < PENDING_MAX_AGE:
                break
            ready.append(row)
        if not ready:
            return 0
        alerts = [row for row in ready if not row["cached"]]
        tpanel_ms = 0.0
        if alerts:
            message = format_message(alerts, ready[0]["id"], ready[-1]["id"])
            stage = "tpanel"
            stage_started = time.monotonic()
            subprocess.run(
                ["/usr/local/bin/tpanel", "say", os.environ.get("ALERTS_TAB", "admin/alerts"), "-"],
                input=message,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                timeout=25,
            )
            tpanel_ms = round((time.monotonic() - stage_started) * 1000, 3)
        stage = "state"
        stage_started = time.monotonic()
        save_state(state, ready[-1]["id"])
        log_result(
            started,
            work_id,
            outcome="ok",
            count=len(alerts),
            processed_count=len(ready),
            id_from=ready[0]["id"],
            id_to=ready[-1]["id"],
            ssh_ms=ssh_ms,
            tpanel_ms=tpanel_ms,
        )
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        exit_code = error.returncode if isinstance(error, subprocess.CalledProcessError) else 1
        reason = (
            "command_failed"
            if isinstance(error, subprocess.CalledProcessError)
            else ("timeout" if isinstance(error, subprocess.TimeoutExpired) else f"{stage}_failure")
        )
        log_result(
            started,
            work_id,
            outcome="error",
            stage=stage,
            exit_code=exit_code,
            reason=reason,
            exception_type=type(error).__name__,
            stage_ms=round((time.monotonic() - stage_started) * 1000, 3),
            stack=[
                f"{frame.filename}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(error.__traceback__)[-12:]
            ],
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
