"""Единый JSON-формат журнала без текстов исключений и ответов провайдера."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

request_id: ContextVar[str] = ContextVar("request_id", default="-")
_APP_PATH = str(Path(__file__).resolve().parent) + "/"


def release_version() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


VERSION = release_version()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def exception_fields(error: BaseException | None) -> dict[str, object]:
    if error is None:
        return {}
    frames = traceback.extract_tb(error.__traceback__)
    last_app_frame = next(
        (frame for frame in reversed(frames) if frame.filename.startswith(_APP_PATH)),
        None,
    )
    kept = frames[-11:]
    if last_app_frame is not None and last_app_frame not in kept:
        kept.insert(0, last_app_frame)
    return {
        "exception_type": type(error).__name__,
        "stack": [f"{frame.filename}:{frame.lineno}:{frame.name}" for frame in kept],
    }


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        fields = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds",
            ),
            "level": record.levelname,
            "service": "alertsbot",
            "version": VERSION,
            "event": getattr(record, "event", "library_log"),
            # Сторонний logger может включать URL с токеном или текст исключения.
            "message": record.getMessage() if record.name.startswith("alertsbot") else record.name,
            "code": f"{Path(record.pathname).parent.name}/{record.filename}:{record.lineno}",
            "request_id": request_id.get(),
        }
        fields.update(getattr(record, "fields", {}))
        if record.exc_info and record.exc_info[1]:
            fields.update(exception_fields(record.exc_info[1]))
        for key, value in fields.items():
            if isinstance(value, str):
                for secret in self.secrets:
                    value = value.replace(secret, "[redacted]")
                fields[key] = value
        return json.dumps(fields, ensure_ascii=True, allow_nan=False)


def configure_logging(level: str, secrets: tuple[str, ...] = ()) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger()
    # Сохраняем сторонние обработчики (в частности захват pytest).
    for existing in root.handlers[:]:
        if isinstance(existing.formatter, JsonFormatter):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        library = logging.getLogger(name)
        library.handlers.clear()
        library.propagate = True
        library.setLevel(logging.WARNING)
