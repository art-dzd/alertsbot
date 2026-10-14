from __future__ import annotations

import io
import json
import re
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

import pytest
from pytest import CaptureFixture, MonkeyPatch

from alertsbot.history import SCHEMA
from scripts import tab_forwarder


def alert(row_id: int, **fields: Any) -> dict[str, Any]:
    return {
        "id": row_id,
        "created_at": "2026-10-14T11:59:00.000+00:00",
        "service": "payments",
        "title": "Не прошла оплата",
        "message": "Проверить\n  платёжный шлюз.",
        "status": "sent",
        "http_status": 200,
        "reason": "",
        "cached": 0,
        **fields,
    }


class Commands:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.max_id = 42
        self.ssh_code = 0
        self.ssh_codes: list[int] = []
        self.reset = False
        self.public_ok = True
        self.now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        self.tpanel_code = 0
        self.calls: list[list[str]] = []
        self.messages: list[str] = []

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["timeout"] > 0
        if command[0] == "ssh":
            assert "BatchMode=yes" in command
            assert "ConnectTimeout=10" in command
            assert command[-2] == "mts"
            code = self.ssh_codes.pop(0) if self.ssh_codes else self.ssh_code
            result = subprocess.CompletedProcess(
                command,
                code,
                json.dumps({"max_id": self.max_id, "rows": self.rows, "reset": self.reset}),
            )
            if kwargs.get("check"):
                result.check_returncode()
            return result
        assert command[:2] == ["/usr/local/bin/tpanel", "say"]
        assert command[-1] == "-"
        self.messages.append(kwargs["input"])
        result = subprocess.CompletedProcess(command, self.tpanel_code, "")
        if kwargs.get("check"):
            result.check_returncode()
        return result


@pytest.fixture
def commands(monkeypatch: MonkeyPatch, tmp_path: Path) -> Commands:
    fake = Commands()

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Clock:
            return cls.fromtimestamp(fake.now.timestamp(), timezone.utc)

    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("ALERTS_TAB", raising=False)
    monkeypatch.setattr(tab_forwarder, "datetime", Clock)

    def public_readyz(url: str, timeout: float) -> io.BytesIO:
        assert url == tab_forwarder.PUBLIC_READYZ
        if not fake.public_ok:
            raise OSError("public path down")
        return io.BytesIO()

    monkeypatch.setattr("urllib.request.urlopen", public_readyz)
    monkeypatch.setattr(subprocess, "run", fake.run)
    return fake


def test_first_run_starts_at_current_max_without_delivery(
    commands: Commands, tmp_path: Path
) -> None:
    assert tab_forwarder.main() == 0
    assert (tmp_path / "last_id").read_text() == "42\n"
    assert len(commands.calls) == 1
    assert commands.messages == []


def test_pending_stops_prefix_and_later_run_delivers_rest(
    commands: Commands, tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    state = tmp_path / "last_id"
    state.write_text("10\n")
    commands.rows = [alert(11), alert(12, status="pending"), alert(13)]
    assert tab_forwarder.main() == 0
    assert state.read_text() == "11\n"
    assert commands.messages == [
        "Алерты: 1 · история id 11–11\n"
        "#11 · 14.10 14:59 МСК · payments · Не прошла оплата · sent\n"
        "Проверить платёжный шлюз."
    ]
    log = json.loads(capsys.readouterr().out)
    assert log["level"] == "INFO"
    assert log["count"] == 1
    assert (log["id_from"], log["id_to"]) == (11, 11)
    assert log["duration_ms"] >= 0
    assert "Не прошла оплата" not in json.dumps(log, ensure_ascii=False)

    commands.rows = [alert(12, status="pending"), alert(13)]
    assert tab_forwarder.main() == 0
    assert state.read_text() == "11\n"
    assert len(commands.messages) == 1
    assert capsys.readouterr().out == ""

    commands.rows[0]["status"] = "sent"
    assert tab_forwarder.main() == 0
    assert state.read_text() == "13\n"
    assert "Алерты: 2 · история id 12–13" in commands.messages[-1]


def test_old_pending_and_exact_five_minutes_do_not_block(
    commands: Commands, tmp_path: Path
) -> None:
    (tmp_path / "last_id").write_text("0\n")
    commands.rows = [
        alert(1, status="pending", created_at="2026-10-14T11:54:00Z"),
        alert(2, status="pending", created_at="2026-10-14T11:55:00+00:00"),
        alert(3, status="failed", http_status=502, reason="telegram_temporary_error"),
        alert(4, status="rejected", service=None, title=None, message=None),
    ]
    assert tab_forwarder.main() == 0
    assert (tmp_path / "last_id").read_text() == "4\n"
    assert "pending (итог неизвестен)" in commands.messages[0]
    assert "failed 502 · telegram_temporary_error" in commands.messages[0]
    assert "rejected" in commands.messages[0]


def test_cached_rows_are_skipped_but_cursor_advances(commands: Commands, tmp_path: Path) -> None:
    state = tmp_path / "last_id"
    state.write_text("1\n")
    commands.rows = [alert(2, cached=1), alert(3), alert(4, cached=1)]
    assert tab_forwarder.main() == 0
    assert state.read_text() == "4\n"
    assert "Алерты: 1 · история id 3–3" in commands.messages[0]
    assert "#2" not in commands.messages[0]
    assert "#4" not in commands.messages[0]

    commands.rows = [alert(5, cached=1)]
    assert tab_forwarder.main() == 0
    assert state.read_text() == "5\n"
    assert len(commands.messages) == 1


@pytest.mark.parametrize("exit_code", [2, 3])
def test_tpanel_failure_keeps_cursor_for_retry(
    commands: Commands, tmp_path: Path, capsys: CaptureFixture[str], exit_code: int
) -> None:
    state = tmp_path / "last_id"
    state.write_text("10\n")
    commands.rows = [alert(11)]
    commands.tpanel_code = exit_code
    assert tab_forwarder.main() != 0
    assert state.read_text() == "10\n"
    log = json.loads(capsys.readouterr().out)
    assert (log["level"], log["stage"], log["exit_code"]) == ("ERROR", "tpanel", exit_code)
    assert log["reason"]

    commands.tpanel_code = 0
    assert tab_forwarder.main() == 0
    assert state.read_text() == "11\n"
    assert commands.messages[0] == commands.messages[1]


def test_message_preview_and_total_size_are_bounded(commands: Commands, tmp_path: Path) -> None:
    (tmp_path / "last_id").write_text("0\n")
    commands.rows = [alert(i, message="я" * 10000) for i in range(1, 31)]
    assert tab_forwarder.main() == 0
    text = commands.messages[0]
    assert len(text) <= 6000
    assert "я" * 300 + "…" in text
    assert "я" * 301 not in text
    displayed = len(re.findall(r"^#\d+ ·", text, re.MULTILINE))
    assert 0 < displayed < 30
    assert f"ещё {30 - displayed} следующей сводкой" in text
    # Курсор только до показанного: остальное придёт следующим проходом, а не пропадёт.
    assert (tmp_path / "last_id").read_text() == f"{displayed}\n"
    commands.rows = commands.rows[displayed:]
    assert tab_forwarder.main() == 0
    assert f"#{displayed + 1} ·" in commands.messages[1]


def test_empty_pass_is_quiet_and_uses_custom_tab(
    commands: Commands, tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    (tmp_path / "last_id").write_text("42\n")
    assert tab_forwarder.main() == 0
    assert commands.messages == []
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("ALERTS_TAB", "admin/test-alerts")
    commands.rows = [alert(43)]
    assert tab_forwarder.main() == 0
    assert commands.calls[-1][2] == "admin/test-alerts"


def test_ssh_failure_is_safe_and_does_not_touch_state(
    commands: Commands, tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    (tmp_path / "last_id").write_text("10\n")
    commands.rows = [alert(11, message="SECRET_ALERT_CONTENT")]
    commands.ssh_code = 255
    assert tab_forwarder.main() != 0
    assert (tmp_path / "last_id").read_text() == "10\n"
    output = capsys.readouterr().out
    log = json.loads(output)
    assert (log["level"], log["stage"], log["exit_code"]) == ("ERROR", "ssh", 255)
    assert "SECRET_ALERT_CONTENT" not in output
    assert commands.messages == []


def test_atomic_state_write_failure_is_safe(
    commands: Commands, tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    state = tmp_path / "last_id"
    state.write_text("10\n")
    commands.rows = [alert(11)]

    def fail_replace(source: str, destination: Path) -> None:
        raise PermissionError("SECRET_EXCEPTION")

    monkeypatch.setattr("scripts.tab_forwarder.os.replace", fail_replace)
    assert tab_forwarder.main() != 0
    assert state.read_text() == "10\n"
    # Курсор не записать — сводку не шлём, иначе она повторялась бы каждую минуту.
    assert commands.messages == []
    output = capsys.readouterr().out
    assert "SECRET_EXCEPTION" not in output
    assert json.loads(output)["stage"] == "state"
    assert list(tmp_path.iterdir()) == [state]


def test_invalid_history_and_ssh_timeout_keep_state(
    commands: Commands, tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    state = tmp_path / "last_id"
    state.write_text("10\n")
    commands.rows = [alert(12), alert(11)]
    assert tab_forwarder.main() != 0
    assert state.read_text() == "10\n"
    assert json.loads(capsys.readouterr().out)["stage"] == "ssh"
    assert commands.messages == []

    def timeout(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 25, output="SECRET", stderr="SECRET")

    monkeypatch.setattr(subprocess, "run", timeout)
    assert tab_forwarder.main() != 0
    assert state.read_text() == "10\n"
    output = capsys.readouterr().out
    assert json.loads(output)["reason"] == "timeout"
    assert "SECRET" not in output


def test_remote_query_reads_real_sqlite_in_id_order_with_limit(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], commands: Commands
) -> None:
    path = tmp_path / "alerts.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(SCHEMA)
        db.executemany(
            "INSERT INTO alerts(id, created_at, client, status) VALUES (?, ?, 'test', 'sent')",
            [(i, "2026-10-14T11:59:00+00:00") for i in range(105, 0, -1)],
        )
    real_connect = sqlite3.connect

    def readonly_connect(database: str, **kwargs: Any) -> sqlite3.Connection:
        assert database == "file:/var/lib/alertsbot/alerts.db?mode=ro"
        assert kwargs["uri"] is True
        assert kwargs == {"uri": True, "timeout": 5}
        return real_connect(f"file:{path}?mode=ro", uri=True, timeout=5)

    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: io.BytesIO())
    (tmp_path / "last_id").write_text("2\n")
    assert tab_forwarder.main() == 0
    capsys.readouterr()
    remote = shlex.split(commands.calls[0][-1])
    assert remote[:5] == ["sudo", "-n", "-u", "alertsbot", "python3"]
    assert remote[5] == "-c"
    monkeypatch.setattr(sys, "argv", ["-c", remote[7]])
    exec(compile(remote[6], "remote_query", "exec"), {})
    data = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in data["rows"]] == list(range(3, 103))

    monkeypatch.setattr(sys, "argv", ["-c", "init"])
    exec(compile(remote[6], "remote_query", "exec"), {})
    assert json.loads(capsys.readouterr().out) == {"max_id": 105, "rows": [], "reset": False}

    monkeypatch.setattr(sys, "argv", ["-c", "500"])
    exec(compile(remote[6], "remote_query", "exec"), {})
    data = json.loads(capsys.readouterr().out)
    assert data["reset"] is True
    assert [row["id"] for row in data["rows"]] == list(range(1, 101))


def test_unreachable_tailnet_falls_back_to_vpn_route(commands: Commands, tmp_path: Path) -> None:
    (tmp_path / "last_id").write_text("10\n")
    commands.rows = [alert(11)]
    commands.ssh_codes = [255]
    assert tab_forwarder.main() == 0
    direct, fallback = commands.calls[:2]
    assert "ProxyJump=none" in direct
    assert "ProxyJump=none" not in fallback
    assert (tmp_path / "last_id").read_text() == "11\n"


def test_not_ready_alertsbot_does_not_try_fallback(commands: Commands, tmp_path: Path) -> None:
    (tmp_path / "last_id").write_text("10\n")
    commands.ssh_code = 3
    assert tab_forwarder.main() != 0
    assert len(commands.calls) == 1


def test_channel_outage_reported_once_and_recovery(
    commands: Commands, tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    (tmp_path / "last_id").write_text("10\n")
    commands.ssh_code = 3
    assert tab_forwarder.main() != 0
    assert commands.messages == []

    commands.now = commands.now.replace(minute=3)
    assert tab_forwarder.main() != 0
    assert len(commands.messages) == 1
    assert "не готов (readyz)" in commands.messages[0]
    assert "14.10 15:00 МСК" in commands.messages[0]

    commands.now = commands.now.replace(minute=10)
    assert tab_forwarder.main() != 0
    assert len(commands.messages) == 1

    commands.ssh_code = 0
    assert tab_forwarder.main() == 0
    assert len(commands.messages) == 2
    assert "снова доступен" in commands.messages[1]
    assert not (tmp_path / "outage").exists()
    capsys.readouterr()


def test_history_reset_rereads_from_start_with_warning(
    commands: Commands, tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    (tmp_path / "last_id").write_text("500\n")
    commands.reset = True
    commands.max_id = 2
    commands.rows = [alert(1), alert(2)]
    assert tab_forwarder.main() == 0
    assert "#1 ·" in commands.messages[0]
    assert (tmp_path / "last_id").read_text() == "2\n"
    log = json.loads(capsys.readouterr().out)
    assert (log["level"], log["reason"]) == ("WARNING", "history_reset")


def test_broken_outage_file_does_not_block_delivery(commands: Commands, tmp_path: Path) -> None:
    (tmp_path / "last_id").write_text("10\n")
    (tmp_path / "outage").write_text("{broken")
    commands.rows = [alert(11)]
    assert tab_forwarder.main() == 0
    assert len(commands.messages) == 1
    assert not (tmp_path / "outage").exists()


def test_public_path_outage_reported_while_history_still_delivered(
    commands: Commands, tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    (tmp_path / "last_id").write_text("10\n")
    commands.public_ok = False
    commands.rows = [alert(11)]
    assert tab_forwarder.main() == 0
    assert len(commands.messages) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "public_unreachable"

    commands.rows = []
    commands.now = commands.now.replace(minute=3)
    assert tab_forwarder.main() == 0
    assert "недоступен снаружи" in commands.messages[1]

    commands.public_ok = True
    assert tab_forwarder.main() == 0
    assert "снова доступен" in commands.messages[2]
    capsys.readouterr()
