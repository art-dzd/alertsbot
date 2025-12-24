"""HTTP API для получения уведомлений и отправки в Telegram."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from alertsbot.config import get_settings
from alertsbot.telegram import send_message


class NotifyRequest(BaseModel):
    """Запрос на отправку уведомления."""

    service: str = Field(..., description="Название сервиса")
    title: str = Field(..., description="Заголовок уведомления")
    message: str = Field(..., description="Основной текст уведомления")
    details: str | None = Field(default=None, description="Дополнительные детали")


app = FastAPI(title="alertsbot")
settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("alertsbot")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Проверка доступности сервиса."""

    return {"status": "ok"}


@app.post("/notify")
async def notify(
    payload: NotifyRequest,
    x_alerts_token: str | None = Header(default=None, alias="X-Alerts-Token"),
) -> dict[str, str]:
    """Отправляет уведомление в Telegram."""

    if not settings.alerts_token or x_alerts_token != settings.alerts_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    text = f"{payload.service}\n{payload.title}\n{payload.message}"
    if payload.details:
        text = f"{text}\n\n{payload.details}"

    try:
        await send_message(
            settings.alerts_bot_token,
            settings.alerts_chat_id,
            text,
            settings.request_timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send Telegram message")
        raise HTTPException(status_code=502, detail="Telegram error")

    return {"status": "sent"}
