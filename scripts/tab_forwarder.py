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
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict, cast
from uuid import uuid4

BATCH_LIMIT = 100
PENDING_MAX_AGE = timedelta(minutes=5)
MESSAGE_PREVIEW_LIMIT = 300
MESSAGE_LIMIT = 6000
MOSCOW = timezone(timedelta(hours=3))
# Худший проход (два ssh-маршрута, внешний readyz, два tpanel) укладывается в TimeoutStartSec=90s.
SSH_TIMEOUT_SECONDS = 15
# 26.10 /readyz отвечал ~11 с; таймаут больше него и меньше SSH_TIMEOUT_SECONDS.
REMOTE_READYZ_TIMEOUT_SECONDS = 12
PUBLIC_TIMEOUT_SECONDS = 10
TPANEL_TIMEOUT_SECONDS = 20
# Этим путём шлют vpn, hr, gitea; его поломку при живом alertsbot иначе не видит никто.
PUBLIC_READYZ = "https://aiomc.ru/alerts/readyz"
OUTAGE_REPORT_AFTER = timedelta(minutes=3)
SSH_UNREACHABLE = 255
ALERTSBOT_NOT_READY = 3
# Сначала напрямую по tailnet, чтобы не зависеть от vpn; запасной путь — ProxyJump из ~/.ssh/config.
SSH_ROUTES = (("-o", "ProxyJump=none", "-o", "HostName=100.89.26.10"), ())
VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
REMOTE_QUERY = f"""
import json
import sqlite3
import sys
import urllib.request
from contextlib import closing

try:
    urllib.request.urlopen(
        'http://127.0.0.1:9100/readyz',
        timeout={REMOTE_READYZ_TIMEOUT_SECONDS},
    ).close()
except Exception:
    sys.exit({ALERTSBOT_NOT_READY})

with closing(sqlite3.connect(
    'file:/var/lib/alertsbot/alerts.db?mode=ro', uri=True, timeout=5
)) as db:
    db.row_factory = sqlite3.Row
    maximum = db.execute('SELECT COALESCE(MAX(id), 0) FROM alerts').fetchone()[0]
    rows = []
    reset = False
    if sys.argv[1] != 'init':
        cursor = int(sys.argv[1])
        # Файл истории пересоздали — id пошли заново, читаем с начала.
        reset = cursor > maximum
        rows = [dict(row) for row in db.execute(
            'SELECT id, created_at, service, title, message, status, http_status, reason, cached '
            'FROM alerts WHERE id > ? ORDER BY id LIMIT ?',
            (0 if reset else cursor, {BATCH_LIMIT})
        )]
    print(json.dumps({{'max_id': maximum, 'rows': rows, 'reset': reset}}, ensure_ascii=True))
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


def fetch_history(last_id: int | None) -> tuple[int, list[Alert], bool, int]:
    """Возвращает max(id), новые строки, признак сброса id и номер ssh-маршрута."""
    cursor = "init" if last_id is None else str(last_id)
    command = f"sudo -n -u alertsbot python3 -c {shlex.quote(REMOTE_QUERY)} {cursor}"
    for route_index, route in enumerate(SSH_ROUTES):
        try:
            response = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *route, "mts", command],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                timeout=SSH_TIMEOUT_SECONDS,
            )
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            unreachable = isinstance(error, subprocess.TimeoutExpired) or (
                error.returncode == SSH_UNREACHABLE
            )
            if not unreachable or route_index == len(SSH_ROUTES) - 1:
                raise
    data = json.loads(response.stdout)
    if not isinstance(data, dict) or type(data.get("max_id")) is not int or data["max_id"] < 0:
        raise ValueError("invalid_history")
    rows = data.get("rows")
    reset = data.get("reset")
    if not isinstance(rows, list) or len(rows) > BATCH_LIMIT or not isinstance(reset, bool):
        raise ValueError("invalid_history")
    previous = 0 if reset else last_id or 0
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
    return data["max_id"], cast(list[Alert], rows), reset, route_index


def compact(value: str | None, limit: int) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def format_message(rows: list[Alert]) -> tuple[str, int]:
    """Сводка из первых алертов в пределах MESSAGE_LIMIT; возвращает текст и число показанных."""
    blocks: list[str] = []
    size = 0
    for row in rows:
        status = row["status"]
        if status == "failed":
            status += f" {row['http_status']}" if row["http_status"] is not None else ""
        elif status == "pending":
            status += " (итог неизвестен)"
        reason = compact(row["reason"], 256)
        block = (
            f"\n#{row['id']} · {moscow_time(row['created_at'])} МСК · "
            f"{compact(row['service'], 128) or '—'} · "
            f"{compact(row['title'], 256) or '—'} · {status}"
            + (f" · {reason}" if reason else "")
            + f"\n{compact(row['message'], MESSAGE_PREVIEW_LIMIT)}"
        )
        if blocks and size + len(block) > MESSAGE_LIMIT:
            break
        blocks.append(block)
        size += len(block)
    shown = len(blocks)
    header = f"Алерты: {shown} · история id {rows[0]['id']}–{rows[shown - 1]['id']}"
    if shown < len(rows):
        header += f" · ещё {len(rows) - shown} следующей сводкой"
    return header + "".join(blocks), shown


def moscow_time(value: str) -> str:
    return parse_time(value).astimezone(MOSCOW).strftime("%d.%m %H:%M")


def say(text: str) -> None:
    subprocess.run(
        ["/usr/local/bin/tpanel", "say", os.environ.get("ALERTS_TAB", "admin/alerts"), "-"],
        input=text,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        timeout=TPANEL_TIMEOUT_SECONDS,
    )


def outage_problem(error: Exception) -> str:
    code = error.returncode if isinstance(error, subprocess.CalledProcessError) else None
    if code == ALERTSBOT_NOT_READY:
        return "alertsbot на mts не готов (readyz) — алерты не принимаются"
    if code == SSH_UNREACHABLE or isinstance(error, subprocess.TimeoutExpired):
        return "mts недоступен по ssh — новые алерты сюда не доходят"
    return "история alertsbot не читается — новые алерты сюда не доходят"


def public_problem() -> str | None:
    try:
        urllib.request.urlopen(PUBLIC_READYZ, timeout=PUBLIC_TIMEOUT_SECONDS).close()
    except (OSError, ValueError):
        return "alertsbot недоступен снаружи (aiomc.ru/alerts) — алерты vpn, hr, gitea теряются"
    return None


def read_outage(path: Path) -> dict[str, Any] | None:
    try:
        outage = json.loads(path.read_text(encoding="utf-8"))
        parse_time(outage["since"])
        if isinstance(outage["reported"], bool):
            return cast(dict[str, Any], outage)
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        pass
    # Битый файл считаем отсутствующим: его перезапишет или удалит этот же проход.
    return None


def note_outage(path: Path, problem: str) -> None:
    """Сбой канала дольше OUTAGE_REPORT_AFTER — одно сообщение во вкладку."""
    now = datetime.now(timezone.utc)
    outage = read_outage(path) or {"since": now.isoformat(), "reported": False}
    if not outage["reported"] and now - parse_time(outage["since"]) >= OUTAGE_REPORT_AFTER:
        say(f"Канал алертов: {problem}. Сбой с {moscow_time(outage['since'])} МСК.")
        outage["reported"] = True
    write_atomic(path, json.dumps(outage))


def end_outage(path: Path) -> None:
    outage = read_outage(path)
    if outage is not None and outage["reported"]:
        say(f"Канал алертов снова доступен, сбой был с {moscow_time(outage['since'])} МСК.")
    path.unlink(missing_ok=True)


def write_atomic(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        try:
            temporary.write(text)
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
                "level": {"error": "ERROR", "fallback": "WARNING"}.get(
                    str(fields.get("outcome")), "INFO"
                ),
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
        state_directory = Path(os.environ["STATE_DIRECTORY"])
        state = state_directory / "last_id"
        try:
            last_id = int(state.read_text(encoding="ascii").strip())
        except FileNotFoundError:
            last_id = None
        if last_id is not None and last_id < 0:
            raise ValueError("invalid_cursor")

        stage = "ssh"
        stage_started = time.monotonic()
        try:
            maximum, rows, reset, route_index = fetch_history(last_id)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
            # Сбой сообщения о сбое пишется в итог с этапом outage, исходный — в его контекст.
            stage = "outage"
            note_outage(state_directory / "outage", outage_problem(error))
            stage = "ssh"
            raise
        ssh_ms = round((time.monotonic() - stage_started) * 1000, 3)
        stage = "public"
        problem = public_problem()
        stage = "outage"
        if problem:
            note_outage(state_directory / "outage", problem)
        else:
            end_outage(state_directory / "outage")
        if last_id is None:
            stage = "state"
            stage_started = time.monotonic()
            write_atomic(state, f"{maximum}\n")
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

        # Деградации пишутся и в пустом проходе: иначе их не видно.
        reasons = (
            ["history_reset"] * reset
            + ["ssh_via_vpn"] * (route_index > 0)
            + ["public_unreachable"] * bool(problem)
        )
        outcome = "fallback" if reasons else "ok"
        if reset:
            last_id = 0
        ready: list[Alert] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            if row["status"] == "pending" and now - parse_time(row["created_at"]) < PENDING_MAX_AGE:
                break
            ready.append(row)
        if not ready:
            if reasons:
                stage = "state"
                write_atomic(state, f"{last_id}\n")
                log_result(
                    started, work_id, outcome=outcome, reason=";".join(reasons), count=0,
                    ssh_ms=ssh_ms,
                )
            return 0
        alerts = [row for row in ready if not row["cached"]]
        tpanel_ms = 0.0
        if alerts:
            message, shown = format_message(alerts)
            if shown < len(alerts):
                alerts = alerts[:shown]
                ready = [row for row in ready if row["id"] <= alerts[-1]["id"]]
            # Курсор не записать — не шлём, иначе та же сводка уйдёт снова через минуту.
            stage = "state"
            write_atomic(state, f"{last_id}\n")
            stage = "tpanel"
            stage_started = time.monotonic()
            say(message)
            tpanel_ms = round((time.monotonic() - stage_started) * 1000, 3)
        stage = "state"
        stage_started = time.monotonic()
        write_atomic(state, f"{ready[-1]['id']}\n")
        log_result(
            started,
            work_id,
            outcome=outcome,
            reason=";".join(reasons),
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
