"""Авторизация, история и синхронная доставка уведомления в Telegram."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from alertsbot.config import get_settings
from alertsbot.history import History
from alertsbot.logging import VERSION, configure_logging, exception_fields, request_id, utc_now
from alertsbot.telegram import (
    DeliveryStats,
    TelegramClientPool,
    TelegramPayloadError,
    TelegramPermanentError,
    send_message,
)

TELEGRAM_MESSAGE_LIMIT = 4096
TRUNCATION_NOTICE = "\n\n[сообщение усечено до лимита Telegram]"
SERVICE_MAX_LENGTH = 128
TITLE_MAX_LENGTH = 256
MESSAGE_MAX_LENGTH = 4096
DETAILS_MAX_LENGTH = 8192
EVENT_ID_MAX_LENGTH = 128
# Поля и так режутся под Telegram; лимит защищает память и историю от мусорного клиента.
BODY_MAX_BYTES = 1024 * 1024
IDEMPOTENCY_TTL_SECONDS = 3600.0
IDEMPOTENCY_MAX_RECORDS = 10_000
CACHEABLE_ERROR_STATUS_CODES = frozenset({422, 424})


@dataclass(frozen=True, slots=True)
class NotifyResult:
    status_code: int
    body: dict[str, str]
    reason: str = ""
    telegram_status: int | None = None
    sent_at: str | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    result: NotifyResult
    expires_at: float


@dataclass(slots=True)
class EventLockEntry:
    lock: asyncio.Lock
    refs: int = 0


class NotifyRequest(BaseModel):
    service: str
    title: str
    message: str
    details: str | None = None
    event_id: str | None = None


configure_logging("INFO")
logger = logging.getLogger("alertsbot")
try:
    settings = get_settings()
except ValidationError as error:
    logger.error(
        "Некорректная конфигурация",
        extra={
            "event": "startup_failed",
            "fields": {"reason": "invalid_configuration", **exception_fields(error)},
        },
    )
    raise SystemExit(1) from None
configure_logging(settings.log_level, (settings.alerts_bot_token, settings.alerts_token))
history = History(settings.db_path)
_idempotency_records: dict[str, IdempotencyRecord] = {}
_idempotency_locks: dict[str, EventLockEntry] = {}
_idempotency_index_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    fastapi_app.state.telegram_client_pool = TelegramClientPool(settings.request_timeout_seconds)
    history.cleanup()
    if VERSION == "unknown":
        logger.error("Версия выпуска неизвестна", extra={"event": "version_unavailable"})
    logger.info("Сервис запущен", extra={"event": "startup"})
    try:
        yield
    finally:
        await fastapi_app.state.telegram_client_pool.aclose()


app = FastAPI(
    title="alertsbot",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
    lifespan=lifespan,
)


def _is_authorized(provided_token: str | None) -> bool:
    return (
        bool(settings.alerts_token)
        and provided_token is not None
        and secrets.compare_digest(
            provided_token.encode(),
            settings.alerts_token.encode(),
        )
    )


def normalize_payload(data: dict[str, Any]) -> tuple[NotifyRequest, list[str]]:
    values: dict[str, Any] = {}
    reasons: list[str] = []
    for field in ("service", "title", "message", "details", "event_id"):
        value = data.get(field)
        if value is None:
            if field in {"details", "event_id"}:
                values[field] = None
                continue
            value = f"[поле {field} отсутствует]"
            reasons.append(f"{field}:missing")
        elif not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=True)
            reasons.append(f"{field}:converted")
        if not value.strip():
            value = None if field in {"details", "event_id"} else f"[поле {field} пустое]"
            reasons.append(f"{field}:empty")
        values[field] = value
    # Полный текст остаётся в истории. Усекается только представление для Telegram.
    for field, limit in (
        ("service", SERVICE_MAX_LENGTH),
        ("title", TITLE_MAX_LENGTH),
        ("message", MESSAGE_MAX_LENGTH),
        ("details", DETAILS_MAX_LENGTH),
    ):
        if values[field] is not None and len(values[field]) > limit:
            reasons.append(f"{field}:truncated")
    if values["event_id"] is not None and len(values["event_id"]) > EVENT_ID_MAX_LENGTH:
        reasons.append("event_id:hashed")
    payload = NotifyRequest(**values)
    if len(_alert_text(payload)) > TELEGRAM_MESSAGE_LIMIT:
        reasons.append("telegram_text:truncated")
    return payload, reasons


def _alert_text(payload: NotifyRequest) -> str:
    text = (
        f"{payload.service[:SERVICE_MAX_LENGTH]}\n{payload.title[:TITLE_MAX_LENGTH]}\n"
        f"{payload.message[:MESSAGE_MAX_LENGTH]}"
    )
    if payload.details:
        text += f"\n\n{payload.details[:DETAILS_MAX_LENGTH]}"
    return text


def format_alert(payload: NotifyRequest) -> str:
    text = _alert_text(payload)
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    return text[: TELEGRAM_MESSAGE_LIMIT - len(TRUNCATION_NOTICE)] + TRUNCATION_NOTICE


def _event_key(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8", errors="surrogatepass")).hexdigest()


def _logged_event_id(event_id: str | None) -> str | None:
    if event_id is not None and len(event_id) > EVENT_ID_MAX_LENGTH:
        return "sha256:" + _event_key(event_id)
    return event_id


def _prune_expired_idempotency_records(now: float) -> None:
    for event_id in list(_idempotency_records):
        if _idempotency_records[event_id].expires_at <= now:
            del _idempotency_records[event_id]


@asynccontextmanager
async def _hold_event_lock(event_id: str) -> AsyncIterator[None]:
    async with _idempotency_index_lock:
        _prune_expired_idempotency_records(time.monotonic())
        entry = _idempotency_locks.get(event_id)
        if entry is None:
            entry = EventLockEntry(lock=asyncio.Lock())
            _idempotency_locks[event_id] = entry
        entry.refs += 1
    try:
        async with entry.lock:
            yield
    finally:
        async with _idempotency_index_lock:
            entry.refs -= 1
            if entry.refs <= 0:
                _idempotency_locks.pop(event_id, None)


def _remember_notify_result(event_id: str, result: NotifyResult) -> None:
    while len(_idempotency_records) >= IDEMPOTENCY_MAX_RECORDS:
        _idempotency_records.pop(next(iter(_idempotency_records)))
    _idempotency_records[event_id] = IdempotencyRecord(
        result=result,
        expires_at=time.monotonic() + IDEMPOTENCY_TTL_SECONDS,
    )


async def _send_notify(payload: NotifyRequest, stats: DeliveryStats) -> NotifyResult:
    started = time.monotonic()
    try:
        await send_message(
            settings.alerts_bot_token,
            settings.alerts_chat_id,
            format_alert(payload),
            settings.telegram_proxy_sequence,
            settings.telegram_proxy_circuit_breaker_seconds,
            client_pool=app.state.telegram_client_pool,
            stats=stats,
        )
    except TelegramPayloadError as error:
        stats.status = error.status_code
        return NotifyResult(
            422, {"detail": "Telegram payload rejected"}, "telegram_payload_rejected", stats.status
        )
    except TelegramPermanentError as error:
        stats.status = error.status_code
        return NotifyResult(
            424, {"detail": "Permanent Telegram error"}, "telegram_permanent_error", stats.status
        )
    except Exception as error:
        stats.exception = error
        if isinstance(error, httpx.HTTPStatusError):
            stats.status = error.response.status_code
        # В итог идёт тип и безопасная причина; URL/текст исключения не выводятся.
        return NotifyResult(
            502, {"detail": "Temporary Telegram error"}, "telegram_temporary_error", stats.status
        )
    finally:
        stats.telegram_ms = round((time.monotonic() - started) * 1000, 3)
    return NotifyResult(200, {"status": "sent"}, telegram_status=stats.status, sent_at=utc_now())


async def _send_notify_once_per_event(payload: NotifyRequest, stats: DeliveryStats) -> NotifyResult:
    if payload.event_id is None:
        return await _send_notify(payload, stats)
    key = _event_key(payload.event_id)
    async with _hold_event_lock(key):
        cached = _idempotency_records.get(key)
        if cached is not None and cached.expires_at > time.monotonic():
            stats.cached = True
            return cached.result
        result = await _send_notify(payload, stats)
        if result.status_code == 200 or result.status_code in CACHEABLE_ERROR_STATUS_CODES:
            _remember_notify_result(key, result)
        return result


@app.get("/healthz")
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def ready() -> dict[str, str]:
    if not history.ready():
        raise HTTPException(status_code=503, detail="Alert history unavailable")
    return {"status": "ready"}


@app.post("/notify")
async def notify(request: Request) -> JSONResponse:
    started = time.monotonic()
    incoming_id = request.headers.get("X-Request-ID", "")
    work_id = incoming_id if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", incoming_id) else uuid4().hex
    context_token = request_id.set(work_id)
    client = request.client.host if request.client else "unknown"
    row_id = None
    payload = None
    reasons: list[str] = []
    stats = DeliveryStats()
    outcome = "error"
    result = NotifyResult(500, {"detail": "Internal error"}, "internal_error")
    try:
        if not _is_authorized(request.headers.get("X-Alerts-Token")):
            result = NotifyResult(401, {"detail": "Unauthorized"}, "unauthorized")
            outcome = "rejected"
        else:
            body = bytearray()
            too_large = False
            async for chunk in request.stream():
                body += chunk
                if len(body) > BODY_MAX_BYTES:
                    too_large = True
                    break
            data = None
            if not too_large:
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    pass
            if too_large:
                row_id = history.insert({}, client, "rejected")
                result = NotifyResult(413, {"detail": "Body too large"}, "body_too_large")
                outcome = "rejected"
            elif not isinstance(data, dict):
                row_id = history.insert({}, client, "rejected")
                result = NotifyResult(
                    422, {"detail": "Expected JSON object"}, "invalid_json_object"
                )
                outcome = "rejected"
            else:
                payload, reasons = normalize_payload(data)
                row_id = history.insert(payload.model_dump(), client)
                if row_id is None:
                    reasons.append("database_failure")
                result = await _send_notify_once_per_event(payload, stats)
                outcome = (
                    "error" if result.status_code >= 400 else "skipped" if stats.cached else "ok"
                )
                if result.status_code == 200:
                    if stats.attempts > 1:
                        reasons.append("telegram_retry")
                    if reasons:
                        outcome = "fallback"
        return JSONResponse(
            result.body, status_code=result.status_code, headers={"X-Request-ID": work_id}
        )
    except asyncio.CancelledError:
        result = NotifyResult(499, {}, "cancelled")
        outcome = "cancelled"
        raise
    except Exception as error:
        stats.exception = error
        return JSONResponse(
            result.body,
            status_code=500,
            headers={"X-Request-ID": work_id},
        )
    finally:
        reason = ";".join(filter(None, [result.reason, *reasons]))
        if row_id is not None:
            updated = history.finish(
                row_id,
                "rejected"
                if outcome == "rejected"
                else "sent"
                if result.status_code == 200
                else "failed",
                result.status_code,
                result.telegram_status,
                reason,
                result.sent_at,
                stats.cached,
            )
            if not updated and result.status_code == 200:
                outcome = "fallback"
                if "database_failure" not in reasons:
                    reason = ";".join(filter(None, [reason, "database_failure"]))
        level = (
            logging.ERROR
            if outcome == "error"
            else (logging.WARNING if outcome in {"fallback", "rejected"} else logging.INFO)
        )
        logger.log(
            level,
            "Запрос уведомления завершён",
            extra={
                "event": "notify_done",
                "fields": {
                    "outcome": outcome,
                    "reason": reason,
                    "history_id": row_id,
                    "event_id": _logged_event_id(payload.event_id) if payload else None,
                    "alert_service": payload.service[:SERVICE_MAX_LENGTH] if payload else None,
                    "client": client,
                    "method": "POST",
                    "route": "/notify",
                    "http_status": result.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "telegram_ms": stats.telegram_ms,
                    "proxy": stats.proxy,
                    "attempts": stats.attempts,
                    "cached": stats.cached,
                    "telegram_status": result.telegram_status,
                    "error_code": stats.error_code,
                    "exception_type": None,
                    **exception_fields(stats.exception),
                },
            },
        )
        request_id.reset(context_token)
