"""HTTP API для получения уведомлений и отправки в Telegram."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from alertsbot.config import get_settings
from alertsbot.telegram import (
    TelegramClientPool,
    TelegramPayloadError,
    TelegramPermanentError,
    describe_proxy,
    describe_telegram_error,
    send_message,
)

TELEGRAM_MESSAGE_LIMIT = 4096
TRUNCATION_NOTICE = "\n\n[сообщение усечено до лимита Telegram]"
SERVICE_MAX_LENGTH = 128
TITLE_MAX_LENGTH = 256
MESSAGE_MAX_LENGTH = 4096
DETAILS_MAX_LENGTH = 8192
EVENT_ID_MAX_LENGTH = 128
IDEMPOTENCY_TTL_SECONDS = 3600.0
IDEMPOTENCY_MAX_RECORDS = 10_000
# Реплеим только постоянные исходы; временные (502) не кэшируем, чтобы повтор
# клиента с тем же event_id делал реальную новую попытку отправки.
CACHEABLE_ERROR_STATUS_CODES = frozenset({422, 424})
QUIET_HEALTH_PATHS = frozenset({"/health", "/healthz", "/readyz"})


class HealthAccessFilter(logging.Filter):
    """Не пишет успешные health-check, но сохраняет их ошибки."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return True
        _client, method, target, _version, status = record.args
        if not isinstance(target, str) or not isinstance(status, int):
            return True
        path = target.partition("?")[0]
        return not (method == "GET" and path in QUIET_HEALTH_PATHS and 200 <= status < 400)


@dataclass(frozen=True, slots=True)
class NotifyResult:
    status_code: int
    body: dict[str, str]


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    result: NotifyResult
    expires_at: float


@dataclass(slots=True)
class EventLockEntry:
    lock: asyncio.Lock
    refs: int = 0


class NotifyRequest(BaseModel):
    """Запрос на отправку уведомления."""

    service: str = Field(
        ...,
        max_length=SERVICE_MAX_LENGTH,
        description="Название сервиса",
    )
    title: str = Field(
        ...,
        max_length=TITLE_MAX_LENGTH,
        description="Заголовок уведомления",
    )
    message: str = Field(
        ...,
        max_length=MESSAGE_MAX_LENGTH,
        description="Основной текст уведомления",
    )
    details: str | None = Field(
        default=None,
        max_length=DETAILS_MAX_LENGTH,
        description="Дополнительные детали",
    )
    event_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=EVENT_ID_MAX_LENGTH,
        description="Идемпотентный идентификатор события",
    )


settings = get_settings()
docs_url = None if settings.is_production else "/docs"
redoc_url = None if settings.is_production else "/redoc"
openapi_url = None if settings.is_production else "/openapi.json"

_idempotency_records: dict[str, IdempotencyRecord] = {}
_idempotency_locks: dict[str, EventLockEntry] = {}
_idempotency_index_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    fastapi_app.state.telegram_client_pool = TelegramClientPool(settings.request_timeout_seconds)
    try:
        yield
    finally:
        await fastapi_app.state.telegram_client_pool.aclose()


app = FastAPI(
    title="alertsbot",
    docs_url=docs_url,
    redoc_url=redoc_url,
    openapi_url=openapi_url,
    lifespan=lifespan,
)
logging.basicConfig(level=settings.log_level)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").addFilter(HealthAccessFilter())
logger = logging.getLogger("alertsbot")


def _is_authorized(provided_token: str | None) -> bool:
    if not settings.alerts_token or provided_token is None:
        return False

    return secrets.compare_digest(provided_token, settings.alerts_token)


def _fit_telegram_message_limit(text: str) -> str:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text

    visible_text_limit = TELEGRAM_MESSAGE_LIMIT - len(TRUNCATION_NOTICE)
    return f"{text[:visible_text_limit]}{TRUNCATION_NOTICE}"


def format_alert(payload: NotifyRequest) -> str:
    text = f"{payload.service}\n{payload.title}\n{payload.message}"
    if payload.details:
        text = f"{text}\n\n{payload.details}"

    return _fit_telegram_message_limit(text)


def _telegram_client_pool() -> TelegramClientPool | None:
    return getattr(app.state, "telegram_client_pool", None)


def _prune_expired_idempotency_records(now: float) -> None:
    expired_event_ids = [
        event_id
        for event_id, record in _idempotency_records.items()
        if record.expires_at <= now
    ]
    for event_id in expired_event_ids:
        _idempotency_records.pop(event_id, None)


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


def _cached_notify_result(event_id: str) -> NotifyResult | None:
    now = time.monotonic()
    _prune_expired_idempotency_records(now)
    record = _idempotency_records.get(event_id)
    if record is None:
        return None

    if record.expires_at <= now:
        _idempotency_records.pop(event_id, None)
        return None

    return record.result


def _remember_notify_result(event_id: str, result: NotifyResult) -> None:
    while len(_idempotency_records) >= IDEMPOTENCY_MAX_RECORDS:
        oldest_event_id = next(iter(_idempotency_records))
        _idempotency_records.pop(oldest_event_id)

    _idempotency_records[event_id] = IdempotencyRecord(
        result=result,
        expires_at=time.monotonic() + IDEMPOTENCY_TTL_SECONDS,
    )


def _replay_notify_result(result: NotifyResult) -> dict[str, str]:
    if result.status_code >= 400:
        raise HTTPException(status_code=result.status_code, detail=result.body["detail"])

    return dict(result.body)


def _exception_result(error: HTTPException) -> NotifyResult:
    return NotifyResult(
        status_code=error.status_code,
        body={"detail": str(error.detail)},
    )


async def _send_notify(payload: NotifyRequest) -> dict[str, str]:
    text = format_alert(payload)

    send_kwargs = {}
    telegram_client_pool = _telegram_client_pool()
    if telegram_client_pool is not None:
        send_kwargs["client_pool"] = telegram_client_pool

    try:
        proxy_url = await send_message(
            settings.alerts_bot_token,
            settings.alerts_chat_id,
            text,
            settings.request_timeout_seconds,
            settings.telegram_proxy_sequence,
            settings.telegram_proxy_circuit_breaker_seconds,
            **send_kwargs,
        )
    except TelegramPayloadError as error:
        logger.error("Telegram rejected payload: %s", describe_telegram_error(error))
        raise HTTPException(status_code=422, detail="Telegram payload rejected") from error
    except TelegramPermanentError as error:
        logger.error("Permanent Telegram error: %s", describe_telegram_error(error))
        raise HTTPException(status_code=424, detail="Permanent Telegram error") from error
    except Exception as error:  # noqa: BLE001
        logger.error("Temporary Telegram error: %s", describe_telegram_error(error))
        raise HTTPException(status_code=502, detail="Temporary Telegram error") from error

    logger.info("Telegram alert sent via %s", describe_proxy(proxy_url))
    return {"status": "sent"}


async def _send_notify_once_per_event(payload: NotifyRequest) -> dict[str, str]:
    if payload.event_id is None:
        return await _send_notify(payload)

    async with _hold_event_lock(payload.event_id):
        cached_result = _cached_notify_result(payload.event_id)
        if cached_result is not None:
            return _replay_notify_result(cached_result)

        try:
            result_body = await _send_notify(payload)
        except HTTPException as error:
            if error.status_code in CACHEABLE_ERROR_STATUS_CODES:
                _remember_notify_result(payload.event_id, _exception_result(error))
            raise

        _remember_notify_result(
            payload.event_id,
            NotifyResult(status_code=200, body=result_body),
        )
        return result_body


@app.get("/healthz")
@app.get("/health")
async def health() -> dict[str, str]:
    """Проверка доступности сервиса."""

    return {"status": "ok"}


@app.get("/readyz")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/notify")
async def notify(
    payload: NotifyRequest,
    x_alerts_token: str | None = Header(default=None, alias="X-Alerts-Token"),
) -> dict[str, str]:
    """Отправляет уведомление в Telegram."""

    if not _is_authorized(x_alerts_token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return await _send_notify_once_per_event(payload)
