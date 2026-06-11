from __future__ import annotations

import asyncio
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


def test_notify_reuses_event_id_without_duplicate_send(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)
    sent_messages: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
    ) -> str:
        sent_messages.append(text)
        return ""

    monkeypatch.setattr(app_module, "send_message", record_message)
    payload = sample_payload() | {"event_id": "event-1"}

    first_response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=payload,
    )
    second_response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=payload,
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert sent_messages == ["billing\nошибка\nупало"]


def test_notify_concurrent_event_id_posts_send_once(monkeypatch: MonkeyPatch) -> None:
    app_module, unused_client = load_notify_app(monkeypatch)
    send_calls = 0

    async def run_race() -> list[int]:
        nonlocal send_calls
        first_send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def record_message(
            token: str,
            chat_id: str,
            text: str,
            timeout: float,
            proxy_urls: Sequence[str],
            circuit_breaker_seconds: float,
            **kwargs: object,
        ) -> str:
            nonlocal send_calls
            send_calls += 1
            first_send_started.set()
            await release_send.wait()
            return ""

        monkeypatch.setattr(app_module, "send_message", record_message)
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            payload = sample_payload() | {"event_id": "event-race"}
            first_request = asyncio.create_task(
                client.post(
                    "/notify",
                    headers={"X-Alerts-Token": "shared-secret"},
                    json=payload,
                ),
            )
            await first_send_started.wait()
            second_request = asyncio.create_task(
                client.post(
                    "/notify",
                    headers={"X-Alerts-Token": "shared-secret"},
                    json=payload,
                ),
            )
            await asyncio.sleep(0)
            release_send.set()
            responses = await asyncio.gather(first_request, second_request)

        return [response.status_code for response in responses]

    status_codes = asyncio.run(run_race())

    unused_client.close()
    assert status_codes == [200, 200]
    assert send_calls == 1


def test_notify_without_event_id_keeps_sending_each_request(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)
    sent_messages: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
    ) -> str:
        sent_messages.append(text)
        return ""

    monkeypatch.setattr(app_module, "send_message", record_message)

    client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=sample_payload(),
    )
    client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=sample_payload(),
    )

    assert sent_messages == ["billing\nошибка\nупало", "billing\nошибка\nупало"]


def test_notify_forgets_event_id_after_ttl(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)
    sent_messages: list[str] = []
    now = 1000.0

    def monotonic() -> float:
        return now

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
    ) -> str:
        sent_messages.append(text)
        return ""

    monkeypatch.setattr(app_module.time, "monotonic", monotonic)
    monkeypatch.setattr(app_module, "send_message", record_message)
    payload = sample_payload() | {"event_id": "event-ttl"}

    client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, json=payload)
    client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, json=payload)
    now += app_module.IDEMPOTENCY_TTL_SECONDS + 1
    client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, json=payload)

    assert sent_messages == ["billing\nошибка\nупало", "billing\nошибка\nупало"]


def test_notify_replays_event_id_error_without_duplicate_send(monkeypatch: MonkeyPatch) -> None:
    app_module, client = load_notify_app(monkeypatch)
    send_calls = 0

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
    ) -> str:
        nonlocal send_calls
        send_calls += 1
        raise TelegramPermanentError("forbidden", status_code=403)

    monkeypatch.setattr(app_module, "send_message", fail_send)
    payload = sample_payload() | {"event_id": "event-error"}

    first_response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=payload,
    )
    second_response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=payload,
    )

    assert first_response.status_code == 424
    assert second_response.status_code == 424
    assert send_calls == 1


def test_notify_passes_lifespan_client_pool_to_sender(monkeypatch: MonkeyPatch) -> None:
    app_module, unused_client = load_notify_app(monkeypatch)
    captured_pools: list[object] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        timeout: float,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
    ) -> str:
        captured_pools.append(kwargs["client_pool"])
        return ""

    monkeypatch.setattr(app_module, "send_message", record_message)

    with TestClient(app_module.app) as client:
        response = client.post(
            "/notify",
            headers={"X-Alerts-Token": "shared-secret"},
            json=sample_payload(),
        )

    unused_client.close()
    assert response.status_code == 200
    assert captured_pools
