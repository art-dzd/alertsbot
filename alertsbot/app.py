"""HTTP API для получения уведомлений и отправки в Telegram."""

from __future__ import annotations

import logging
import secrets

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from alertsbot.config import get_settings
from alertsbot.telegram import describe_proxy, describe_telegram_error, send_message

TELEGRAM_MESSAGE_LIMIT = 4096
TRUNCATION_NOTICE = "\n\n[сообщение усечено до лимита Telegram]"
SERVICE_MAX_LENGTH = 128
TITLE_MAX_LENGTH = 256
MESSAGE_MAX_LENGTH = 4096
DETAILS_MAX_LENGTH = 8192


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


settings = get_settings()
docs_url = None if settings.is_production else "/docs"
redoc_url = None if settings.is_production else "/redoc"
openapi_url = None if settings.is_production else "/openapi.json"
app = FastAPI(
    title="alertsbot",
    docs_url=docs_url,
    redoc_url=redoc_url,
    openapi_url=openapi_url,
)
logging.basicConfig(level=settings.log_level)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
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


@app.get("/healthz")
@app.get("/health")
async def health() -> dict[str, str]:
    """Проверка доступности сервиса."""

    return {"status": "ok"}


@app.post("/notify")
async def notify(
    payload: NotifyRequest,
    x_alerts_token: str | None = Header(default=None, alias="X-Alerts-Token"),
) -> dict[str, str]:
    """Отправляет уведомление в Telegram."""

    if not _is_authorized(x_alerts_token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    text = format_alert(payload)

    try:
        proxy_url = await send_message(
            settings.alerts_bot_token,
            settings.alerts_chat_id,
            text,
            settings.request_timeout_seconds,
            settings.telegram_proxy_sequence,
            settings.telegram_proxy_circuit_breaker_seconds,
        )
    except Exception as error:  # noqa: BLE001
        logger.error("Failed to send Telegram message: %s", describe_telegram_error(error))
        raise HTTPException(status_code=502, detail="Telegram error") from error

    logger.info("Telegram alert sent via %s", describe_proxy(proxy_url))
    return {"status": "sent"}
