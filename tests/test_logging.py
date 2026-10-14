from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from alertsbot.logging import VERSION, JsonFormatter, request_id


def test_json_log_has_required_fields_and_safe_exception_stack() -> None:
    context = request_id.set("request-123")
    try:
        try:
            raise ValueError("exception secret and personal data")
        except ValueError:
            record = logging.LogRecord(
                "alertsbot",
                logging.ERROR,
                __file__,
                24,
                "Безопасная причина",
                (),
                sys.exc_info(),
            )
        line = JsonFormatter().format(record)
    finally:
        request_id.reset(context)
    data = json.loads(line)
    assert "\n" not in line
    assert data["ts"].endswith("+00:00")
    assert data["level"] == "ERROR"
    assert data["service"] == "alertsbot"
    assert data["version"] == VERSION
    assert data["event"] == "library_log"
    assert data["code"].endswith("test_logging.py:24")
    assert data["request_id"] == "request-123"
    assert data["exception_type"] == "ValueError"
    assert data["stack"]
    assert "personal data" not in line
    assert "exception secret" not in line


def test_formatter_redacts_tokens_and_library_messages() -> None:
    record = logging.LogRecord("alertsbot", logging.WARNING, __file__, 1, "safe", (), None)
    record.fields = {"alert_title": "token=SECRET"}
    formatter = JsonFormatter(("SECRET",))
    assert "SECRET" not in formatter.format(record)
    record.name = "httpx"
    record.msg = "URL /botSECRET/ exception contents"
    line = formatter.format(record)
    assert json.loads(line)["message"] == "httpx"
    assert "exception contents" not in line


@pytest.mark.parametrize(
    "records, expected",
    [
        ([{"event": "startup", "version": "123456abcdef", "level": "INFO"}], 0),
        ([{"event": "startup", "version": "old", "level": "INFO"}], 1),
        (
            [
                {"event": "history_error", "level": "ERROR"},
                {"event": "startup", "version": "123456abcdef", "level": "INFO"},
            ],
            1,
        ),
        (
            [
                {"event": "startup", "version": "123456abcdef", "level": "INFO"},
                {"event": "notify_done", "level": "ERROR"},
            ],
            0,
        ),
        ([{"event": "startup", "version": "123456abcdef", "level": "CRITICAL"}], 1),
        ([], 1),
        (["unparsed"], 1),
    ],
)
def test_ci_startup_gate(records: list[Any], expected: int) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_startup.py"
    result = subprocess.run(
        [sys.executable, str(script), "123456abcdef"],
        input="".join(json.dumps(record) + "\n" for record in records),
        text=True,
        capture_output=True,
    )
    assert result.returncode == expected
