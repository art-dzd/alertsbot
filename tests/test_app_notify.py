from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Sequence
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import LogCaptureFixture, MonkeyPatch

from alertsbot.config import get_settings
from alertsbot.telegram import TelegramPayloadError, TelegramPermanentError, TelegramRateLimitError


def load_notify_app(monkeypatch: MonkeyPatch) -> tuple[Any, TestClient]:
    monkeypatch.setenv("ALERTS_ENV", "prod")
    monkeypatch.setenv("ALERTS_TOKEN", "shared-secret")
    monkeypatch.setenv("ALERTS_BOT_TOKEN", "123456:VERY_SECRET_TOKEN")
    monkeypatch.setenv("ALERTS_CHAT_ID", "-100")
    get_settings.cache_clear()
    sys.modules.pop("alertsbot.app", None)
    app_module = importlib.import_module("alertsbot.app")
    app: FastAPI = app_module.app
    return app_module, TestClient(app)


def create_http_status_error(status_code: int, url: str) -> httpx.HTTPStatusError:
    response = httpx.Response(status_code, request=httpx.Request("POST", url))
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        return error

    raise AssertionError("Expected HTTPStatusError")


def sample_payload(details: str | None = None) -> dict[str, str]:
    payload = {
        "service": "billing",
        "title": "ошибка",
        "message": "упало",
    }
    if details is not None:
        payload["details"] = details

    return payload


def test_notify_accepts_valid_token(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)
    sent_messages: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
    ) -> str:
        sent_messages.append(text)
        return ""

    monkeypatch.setattr(app_module, "send_message", record_message)

    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=sample_payload(),
    )

    assert response.status_code == 200
    assert sent_messages == ["billing\nошибка\nупало"]


def test_notify_rejects_invalid_token(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)
    send_was_called = False

    async def record_call(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
    ) -> str:
        nonlocal send_was_called
        send_was_called = True
        return ""

    monkeypatch.setattr(app_module, "send_message", record_call)

    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "wrong-secret"},
        json=sample_payload(),
    )

    assert response.status_code == 401
    assert send_was_called is False


def test_notify_truncates_long_details_before_telegram(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)
    sent_messages: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
    ) -> str:
        sent_messages.append(text)
        return ""

    monkeypatch.setattr(app_module, "send_message", record_message)

    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=sample_payload(details="x" * 5000),
    )

    sent_text = sent_messages[0]
    assert response.status_code == 200
    assert len(sent_text) == app_module.TELEGRAM_MESSAGE_LIMIT
    assert app_module.TRUNCATION_NOTICE in sent_text


def test_notify_rejects_field_over_limit(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
    ) -> str:
        return ""

    monkeypatch.setattr(app_module, "send_message", record_message)

    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=sample_payload(
            details="x" * (app_module.DETAILS_MAX_LENGTH + 1),
        ),
    )

    assert response.status_code == 422


def test_notify_logs_http_status_without_bot_token(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    app_module, client = load_notify_app(monkeypatch)
    secret_token = "123456:VERY_SECRET_TOKEN"
    telegram_error = create_http_status_error(
        502,
        f"https://api.telegram.org/bot{secret_token}/sendMessage",
    )

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
    ) -> str:
        raise telegram_error

    monkeypatch.setattr(app_module, "send_message", fail_send)

    with caplog.at_level(logging.ERROR, logger="alertsbot"):
        response = client.post(
            "/notify",
            headers={"X-Alerts-Token": "shared-secret"},
            json=sample_payload(),
        )

    assert response.status_code == 502
    assert secret_token not in caplog.text
    assert "HTTPStatusError status=502" in caplog.text


def test_notify_maps_telegram_payload_error_to_422(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
    ) -> str:
        raise TelegramPayloadError("bad request", status_code=400)

    monkeypatch.setattr(app_module, "send_message", fail_send)

    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=sample_payload(),
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Telegram payload rejected"}


def test_notify_maps_telegram_permanent_error_to_424(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
    ) -> str:
        raise TelegramPermanentError("forbidden", status_code=403)

    monkeypatch.setattr(app_module, "send_message", fail_send)

    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=sample_payload(),
    )

    assert response.status_code == 424
    assert response.json() == {"detail": "Permanent Telegram error"}


def test_notify_maps_exhausted_telegram_rate_limit_to_502(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
    ) -> str:
        raise TelegramRateLimitError(
            "rate limit",
            status_code=429,
            retry_after_seconds=30.0,
        )

    monkeypatch.setattr(app_module, "send_message", fail_send)

    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=sample_payload(),
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Temporary Telegram error"}
