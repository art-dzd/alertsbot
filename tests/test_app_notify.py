from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import LogCaptureFixture, MonkeyPatch

from alertsbot.config import get_settings
from alertsbot.telegram import TelegramPayloadError, TelegramPermanentError, TelegramRateLimitError


@pytest.fixture
def notify_app(monkeypatch: MonkeyPatch) -> Iterator[tuple[Any, TestClient]]:
    monkeypatch.setenv("ALERTS_ENV", "prod")
    monkeypatch.setenv("ALERTS_TOKEN", "shared-secret")
    monkeypatch.setenv("ALERTS_BOT_TOKEN", "123456:VERY_SECRET_TOKEN")
    monkeypatch.setenv("ALERTS_CHAT_ID", "-100")
    get_settings.cache_clear()
    sys.modules.pop("alertsbot.app", None)
    app_module = importlib.import_module("alertsbot.app")
    app: FastAPI = app_module.app
    with TestClient(app) as client:
        yield app_module, client


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


def test_notify_accepts_valid_token(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    sent_messages: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
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


def test_notify_rejects_invalid_token(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    send_was_called = False

    async def record_call(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
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


def test_notify_truncates_long_details_before_telegram(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    sent_messages: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
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


def test_notify_truncates_field_over_limit(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
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

    assert response.status_code == 200


def test_notify_logs_http_status_without_bot_token(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
    notify_app: tuple[Any, TestClient],
) -> None:
    app_module, client = notify_app
    secret_token = "123456:VERY_SECRET_TOKEN"
    telegram_error = create_http_status_error(
        502,
        f"https://api.telegram.org/bot{secret_token}/sendMessage",
    )

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
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
    done = [r for r in caplog.records if getattr(r, "event", None) == "notify_done"]
    assert len(done) == 1
    assert vars(done[0])["fields"]["exception_type"] == "HTTPStatusError"
    assert vars(done[0])["fields"]["telegram_status"] == 502


def test_notify_maps_telegram_payload_error_to_422(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
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


def test_notify_maps_telegram_permanent_error_to_424(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
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


def test_notify_maps_exhausted_telegram_rate_limit_to_502(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
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


def test_notify_reuses_event_id_without_duplicate_send(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    sent_messages: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
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


def test_notify_concurrent_event_id_posts_send_once(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, unused_client = notify_app
    send_calls = 0

    async def run_race() -> list[int]:
        nonlocal send_calls
        first_send_started = asyncio.Event()
        release_send = asyncio.Event()
        second_recorded = asyncio.Event()
        original_insert = app_module.history.insert
        insert_count = 0

        def insert(*args: Any, **kwargs: Any) -> int | None:
            nonlocal insert_count
            row_id: int | None = original_insert(*args, **kwargs)
            insert_count += 1
            if insert_count == 2:
                second_recorded.set()
            return row_id

        monkeypatch.setattr(app_module.history, "insert", insert)

        async def record_message(
            token: str,
            chat_id: str,
            text: str,
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
            await second_recorded.wait()
            release_send.set()
            responses = await asyncio.gather(first_request, second_request)

        return [response.status_code for response in responses]

    status_codes = asyncio.run(run_race())

    unused_client.close()
    assert status_codes == [200, 200]
    assert send_calls == 1


def test_notify_without_event_id_keeps_sending_each_request(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    sent_messages: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
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


def test_notify_forgets_event_id_after_ttl(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    sent_messages: list[str] = []
    now = 1000.0

    def monotonic() -> float:
        return now

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
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


def test_notify_replays_event_id_error_without_duplicate_send(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    send_calls = 0

    async def fail_send(
        token: str,
        chat_id: str,
        text: str,
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


def test_notify_passes_lifespan_client_pool_to_sender(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, unused_client = notify_app
    captured_pools: list[object] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
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


def test_notify_retries_event_id_after_temporary_error(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    send_calls = 0

    async def fail_then_send(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
    ) -> str:
        nonlocal send_calls
        send_calls += 1
        if send_calls == 1:
            raise httpx.ConnectError("telegram down")

        return ""

    monkeypatch.setattr(app_module, "send_message", fail_then_send)
    payload = sample_payload() | {"event_id": "event-temporary"}

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

    assert first_response.status_code == 502
    assert second_response.status_code == 200
    assert send_calls == 2


def test_notify_replays_event_id_payload_error_without_duplicate_send(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
) -> None:
    app_module, client = notify_app
    send_calls = 0

    async def reject_payload(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
    ) -> str:
        nonlocal send_calls
        send_calls += 1
        raise TelegramPayloadError("bad payload", status_code=400)

    monkeypatch.setattr(app_module, "send_message", reject_payload)
    payload = sample_payload() | {"event_id": "event-payload"}

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

    assert first_response.status_code == 422
    assert second_response.status_code == 422
    assert send_calls == 1


def test_notify_evicts_oldest_event_id_when_cache_is_full(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    sent_event_ids: list[str] = []

    async def record_message(
        token: str,
        chat_id: str,
        text: str,
        proxy_urls: Sequence[str],
        circuit_breaker_seconds: float,
        **kwargs: object,
    ) -> str:
        sent_event_ids.append(text.splitlines()[0])
        return ""

    monkeypatch.setattr(app_module, "send_message", record_message)
    monkeypatch.setattr(app_module, "IDEMPOTENCY_MAX_RECORDS", 2)

    for event_id in ("event-1", "event-2", "event-3"):
        payload = sample_payload() | {"service": event_id, "event_id": event_id}
        client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, json=payload)

    evicted_payload = sample_payload() | {"service": "event-1", "event_id": "event-1"}
    cached_payload = sample_payload() | {"service": "event-3", "event_id": "event-3"}
    client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, json=evicted_payload)
    client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, json=cached_payload)

    assert sent_event_ids == ["event-1", "event-2", "event-3", "event-1"]
    assert len(app_module._idempotency_records) == 2


def test_notify_cleans_event_lock_after_unhandled_error(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app

    async def explode(payload: object, stats: object) -> dict[str, str]:
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(app_module, "_send_notify", explode)
    payload = sample_payload() | {"event_id": "event-crash"}

    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json=payload,
    )
    assert response.status_code == 500
    assert response.headers["X-Request-ID"]
    assert history_rows(app_module)[0]["status"] == "failed"

    assert app_module._idempotency_locks == {}
    assert "event-crash" not in app_module._idempotency_records


def test_authorization_precedes_json_parsing(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    _, client = notify_app
    response = client.post("/notify", content="not json")
    assert response.status_code == 401


def history_rows(app_module: Any) -> list[dict[str, Any]]:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(app_module.history.path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM alerts ORDER BY id")]


def test_history_is_committed_before_send_and_updated_after(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
) -> None:
    app_module, client = notify_app

    async def send(*args: object, **kwargs: Any) -> str:
        rows = history_rows(app_module)
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert rows[0]["message"] == "упало"
        assert rows[0]["event_id"] == "event-history"
        assert rows[0]["sent_at"] is None
        assert rows[0]["created_at"].endswith("+00:00")
        kwargs["stats"].status = 200
        kwargs["stats"].attempts = 1
        kwargs["stats"].proxy = "direct"
        return ""

    monkeypatch.setattr(app_module, "send_message", send)
    payload = sample_payload() | {"event_id": "event-history"}
    response = client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, json=payload)
    assert response.status_code == 200
    row = history_rows(app_module)[0]
    assert (row["status"], row["http_status"], row["telegram_status"]) == ("sent", 200, 200)
    assert row["sent_at"].endswith("+00:00")
    assert row["client"] == "testclient"

    assert (
        client.post(
            "/notify", headers={"X-Alerts-Token": "shared-secret"}, json=payload
        ).status_code
        == 200
    )
    rows = history_rows(app_module)
    assert len(rows) == 2
    assert rows[1]["cached"] == 1
    assert rows[1]["sent_at"] == row["sent_at"]


@pytest.mark.parametrize("fail_on_update", [False, True])
def test_database_failure_preserves_delivery(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
    caplog: LogCaptureFixture,
    fail_on_update: bool,
) -> None:
    app_module, client = notify_app
    calls = 0
    original_path = app_module.history.path

    async def send(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if fail_on_update:
            app_module.history.path = str(Path(original_path).parent)
        return ""

    monkeypatch.setattr(app_module, "send_message", send)
    if not fail_on_update:
        app_module.history.path = str(Path(original_path).parent)
    response = client.post(
        "/notify", headers={"X-Alerts-Token": "shared-secret"}, json=sample_payload()
    )
    assert response.status_code == 200
    assert calls == 1
    errors = [r for r in caplog.records if getattr(r, "event", None) == "history_error"]
    assert errors and all(r.levelno == logging.ERROR for r in errors)
    done = [r for r in caplog.records if getattr(r, "event", None) == "notify_done"]
    assert len(done) == 1
    assert vars(done[0])["fields"]["outcome"] == "fallback"
    assert "database_failure" in vars(done[0])["fields"]["reason"]


@pytest.mark.parametrize("body", ["not json", "[]", '"private text"', "null", "5"])
def test_non_object_body_is_rejected_and_recorded(
    notify_app: tuple[Any, TestClient],
    caplog: LogCaptureFixture,
    body: str,
) -> None:
    app_module, client = notify_app
    response = client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, content=body)
    assert response.status_code == 422
    row = history_rows(app_module)[0]
    assert (row["status"], row["http_status"], row["reason"]) == (
        "rejected",
        422,
        "invalid_json_object",
    )
    assert row["message"] is None
    done = [r for r in caplog.records if getattr(r, "event", None) == "notify_done"]
    assert len(done) == 1
    assert done[0].levelno == logging.WARNING
    assert "private text" not in caplog.text


def test_oversized_body_is_rejected_and_recorded(notify_app: tuple[Any, TestClient]) -> None:
    app_module, client = notify_app
    body = '{"service": "x", "message": "' + "a" * app_module.BODY_MAX_BYTES + '"}'
    response = client.post("/notify", headers={"X-Alerts-Token": "shared-secret"}, content=body)
    assert response.status_code == 413
    row = history_rows(app_module)[0]
    assert (row["status"], row["http_status"], row["reason"]) == (
        "rejected",
        413,
        "body_too_large",
    )
    assert row["message"] is None


@pytest.mark.parametrize("token", [None, "wrong-secret", ""])
def test_unauthorized_requests_have_no_history(
    notify_app: tuple[Any, TestClient],
    caplog: LogCaptureFixture,
    token: str | None,
) -> None:
    app_module, client = notify_app
    headers = {} if token is None else {"X-Alerts-Token": token}
    response = client.post("/notify", headers=headers, content="broken json")
    assert response.status_code == 401
    assert history_rows(app_module) == []
    done = [r for r in caplog.records if getattr(r, "event", None) == "notify_done"]
    assert len(done) == 1
    assert done[0].levelno == logging.WARNING
    assert vars(done[0])["fields"]["client"] == "testclient"


def test_empty_configured_secret_denies_request(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
) -> None:
    app_module, client = notify_app
    monkeypatch.setattr(app_module.settings, "alerts_token", "")
    response = client.post("/notify", headers={"X-Alerts-Token": ""}, json=sample_payload())
    assert response.status_code == 401
    assert history_rows(app_module) == []


def test_missing_and_non_string_fields_are_delivered_with_warning(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
    caplog: LogCaptureFixture,
) -> None:
    app_module, client = notify_app
    messages: list[str] = []

    async def send(token: str, chat: str, text: str, *args: object, **kwargs: object) -> str:
        messages.append(text)
        return ""

    monkeypatch.setattr(app_module, "send_message", send)
    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret"},
        json={"service": 12, "details": ["detail"]},
    )
    assert response.status_code == 200
    assert messages == ['12\n[поле title отсутствует]\n[поле message отсутствует]\n\n["detail"]']
    done = [r for r in caplog.records if getattr(r, "event", None) == "notify_done"]
    assert len(done) == 1
    assert done[0].levelno == logging.WARNING
    assert vars(done[0])["fields"]["outcome"] == "fallback"
    assert "title:missing" in vars(done[0])["fields"]["reason"]


def test_long_event_ids_do_not_merge_and_history_keeps_full_text(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
    caplog: LogCaptureFixture,
) -> None:
    app_module, client = notify_app
    messages: list[str] = []

    async def send(token: str, chat: str, text: str, *args: object, **kwargs: object) -> str:
        messages.append(text)
        return ""

    monkeypatch.setattr(app_module, "send_message", send)
    payload = {
        "service": "s" * 200,
        "title": "t" * 300,
        "message": "m" * 5000,
        "details": "d" * 9000,
    }
    ids = ["x" * 1000 + "1", "x" * 1000 + "2"]
    for event_id in [ids[0], ids[1], ids[0]]:
        assert (
            client.post(
                "/notify",
                headers={"X-Alerts-Token": "shared-secret"},
                json=payload | {"event_id": event_id},
            ).status_code
            == 200
        )
    assert len(messages) == 2
    assert all(len(message) <= 4096 for message in messages)
    assert messages[0].splitlines()[0] == "s" * 128
    assert messages[0].splitlines()[1] == "t" * 256
    rows = history_rows(app_module)
    assert len(rows) == 3
    assert rows[0]["message"] == payload["message"]
    assert rows[0]["details"] == payload["details"]
    assert rows[0]["event_id"] == ids[0]
    done = [r for r in caplog.records if getattr(r, "event", None) == "notify_done"]
    assert all(vars(r)["fields"]["outcome"] == "fallback" for r in done)
    assert vars(done[0])["fields"]["event_id"] != vars(done[1])["fields"]["event_id"]
    assert vars(done[2])["fields"]["cached"] is True


@pytest.mark.parametrize(
    "error, status",
    [
        (TelegramPayloadError("private", status_code=400), 422),
        (TelegramPermanentError("private", status_code=403), 424),
        (httpx.ConnectError("private"), 502),
    ],
)
def test_history_records_failed_delivery(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
    error: Exception,
    status: int,
) -> None:
    app_module, client = notify_app

    async def fail(*args: object, **kwargs: object) -> str:
        raise error

    monkeypatch.setattr(app_module, "send_message", fail)
    response = client.post(
        "/notify", headers={"X-Alerts-Token": "shared-secret"}, json=sample_payload()
    )
    assert response.status_code == status
    row = history_rows(app_module)[0]
    assert row["status"] == "failed"
    assert row["http_status"] == status
    assert row["sent_at"] is None
    assert "private" not in row["reason"]


def test_json_stdout_request_id_and_quiet_health(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    app_module, client = notify_app
    app_module.configure_logging(app_module.settings.log_level)

    async def send(*args: object, **kwargs: object) -> str:
        return ""

    monkeypatch.setattr(app_module, "send_message", send)
    client.get("/healthz")
    client.get("/readyz")
    assert capsys.readouterr().out == ""
    response = client.post(
        "/notify",
        headers={"X-Alerts-Token": "shared-secret", "X-Request-ID": "incoming-123"},
        json=sample_payload(),
    )
    assert response.headers["X-Request-ID"] == "incoming-123"
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    done = [record for record in records if record["event"] == "notify_done"]
    assert len(records) == len(done) == 1
    assert done[0]["request_id"] == "incoming-123"
    assert done[0]["history_id"] == history_rows(app_module)[0]["id"]
    assert done[0]["service"] == "alertsbot"
    assert done[0]["alert_service"] == "billing"
    assert done[0]["http_status"] == 200
    assert done[0]["outcome"] == "ok"
    assert "упало" not in str(records)


def test_ready_reports_database_failure(
    monkeypatch: MonkeyPatch,
    notify_app: tuple[Any, TestClient],
) -> None:
    app_module, client = notify_app
    monkeypatch.setattr(app_module.history, "path", str(Path(app_module.history.path).parent))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503


def test_real_sender_retry_metadata_and_safe_provider_error(
    notify_app: tuple[Any, TestClient],
    caplog: LogCaptureFixture,
) -> None:
    import json

    app_module, client = notify_app
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == (
            "https://api.telegram.org/bot123456:VERY_SECRET_TOKEN/sendMessage"
        )
        assert json.loads(request.content) == {
            "chat_id": "-100",
            "text": "billing\nошибка\nупало",
            "disable_web_page_preview": True,
        }
        if calls == 1:
            return httpx.Response(502, json={"error_code": 500, "description": "private response"})
        return httpx.Response(200, json={"ok": True})

    app_module.app.state.telegram_client_pool._clients[""] = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    )
    response = client.post(
        "/notify", headers={"X-Alerts-Token": "shared-secret"}, json=sample_payload()
    )
    assert response.status_code == 200
    assert calls == 2
    retries = [r for r in caplog.records if getattr(r, "event", None) == "telegram_retry"]
    assert len(retries) == 1
    assert vars(retries[0])["fields"]["attempt"] == 1
    assert vars(retries[0])["fields"]["status"] == 502
    assert vars(retries[0])["fields"]["error_code"] == 500
    assert vars(retries[0])["fields"]["duration_ms"] >= 0
    done = [r for r in caplog.records if getattr(r, "event", None) == "notify_done"]
    assert len(done) == 1
    fields = vars(done[0])["fields"]
    assert (fields["outcome"], fields["attempts"], fields["proxy"]) == ("fallback", 2, "direct")
    assert fields["telegram_ms"] >= 0
    assert "private response" not in caplog.text
    assert "VERY_SECRET_TOKEN" not in caplog.text


def test_slow_history_insert_does_not_block_second_notify_or_health(
    monkeypatch: MonkeyPatch, notify_app: tuple[Any, TestClient]
) -> None:
    app_module, client = notify_app
    insert_started = threading.Event()
    release_insert = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def slow_insert(
        payload: dict[str, Any], client_name: str, status: str = "pending"
    ) -> int:
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            insert_started.set()
            assert release_insert.wait(5)
            return 1
        return 2

    def fake_finish(*args: object, **kwargs: object) -> bool:
        return True

    async def fake_send(*args: object, **kwargs: object) -> str:
        return ""

    monkeypatch.setattr(app_module.history, "insert", slow_insert)
    monkeypatch.setattr(app_module.history, "finish", fake_finish)
    monkeypatch.setattr(app_module, "send_message", fake_send)

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(
            client.post,
            "/notify",
            headers={"X-Alerts-Token": "shared-secret"},
            json=sample_payload(),
        )
        assert insert_started.wait(2)
        second = pool.submit(
            client.post,
            "/notify",
            headers={"X-Alerts-Token": "shared-secret"},
            json=sample_payload(),
        )
        health = pool.submit(client.get, "/healthz")
        try:
            assert second.result(timeout=2).status_code == 200
            assert health.result(timeout=2).status_code == 200
        finally:
            release_insert.set()
        assert first.result(timeout=5).status_code == 200
