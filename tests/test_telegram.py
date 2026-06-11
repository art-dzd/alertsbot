from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from pytest import LogCaptureFixture, MonkeyPatch, raises

from alertsbot import telegram
from alertsbot.config import Settings

FakeOutcome = Exception | int | tuple[int, dict[str, Any]]


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        url: str,
        json_data: dict[str, Any] | None = None,
    ) -> None:
        request = httpx.Request("POST", url)
        if json_data is None:
            self.response = httpx.Response(status_code, request=request)
        else:
            self.response = httpx.Response(status_code, request=request, json=json_data)

    @property
    def status_code(self) -> int:
        return self.response.status_code

    def json(self) -> Any:
        return self.response.json()

    def raise_for_status(self) -> None:
        self.response.raise_for_status()


class FakeAsyncClient:
    calls: list[str] = []
    outcomes: list[FakeOutcome] = []
    created_proxies: list[str] = []

    def __init__(
        self,
        *,
        timeout: float,
        trust_env: bool,
        proxy: str = "",
        limits: object | None = None,
    ) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.trust_env = trust_env
        self.created_proxies.append(proxy)

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        self.calls.append(self.proxy)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        if isinstance(outcome, tuple):
            status_code, json_data = outcome
            return FakeResponse(status_code, url, json_data)

        return FakeResponse(outcome, url)


def setup_fake_client(monkeypatch: MonkeyPatch, outcomes: list[FakeOutcome]) -> None:
    telegram.reset_proxy_circuit_breakers()
    FakeAsyncClient.calls = []
    FakeAsyncClient.created_proxies = []
    FakeAsyncClient.outcomes = outcomes
    monkeypatch.setattr("alertsbot.telegram.httpx.AsyncClient", FakeAsyncClient)


def send_for_test(
    proxy_urls: tuple[str, ...],
    token: str = "token",
    client_pool: telegram.TelegramClientPool | None = None,
) -> str:
    return asyncio.run(
        telegram.send_message(
            token,
            "chat",
            "text",
            1.0,
            proxy_urls,
            60.0,
            client_pool=client_pool,
        ),
    )


def test_settings_prefers_proxy_url_list() -> None:
    settings = Settings(
        TELEGRAM_PROXY_URL="http://old-proxy:8888",
        TELEGRAM_PROXY_URLS=" http://primary:8888, http://reserve:8888 ",
    )

    assert settings.telegram_proxy_sequence == ("http://primary:8888", "http://reserve:8888")


def test_settings_keeps_single_proxy_backward_compatibility() -> None:
    settings = Settings(TELEGRAM_PROXY_URL="http://single-proxy:8888")

    assert settings.telegram_proxy_sequence == ("http://single-proxy:8888",)


def test_send_message_falls_back_after_connect_error(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [httpx.ConnectError("primary down"), 200])

    used_proxy = send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert used_proxy == "http://reserve:8888"
    assert FakeAsyncClient.calls == ["http://primary:8888", "http://reserve:8888"]


def test_send_message_retries_final_proxy_once(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(
        monkeypatch,
        [httpx.ConnectError("primary down"), httpx.ConnectError("reserve blip"), 200],
    )

    used_proxy = send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert used_proxy == "http://reserve:8888"
    assert FakeAsyncClient.calls == [
        "http://primary:8888",
        "http://reserve:8888",
        "http://reserve:8888",
    ]


def test_send_message_uses_circuit_breaker_after_failure(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [httpx.ConnectError("primary down"), 200])
    send_for_test(("http://primary:8888", "http://reserve:8888"))

    FakeAsyncClient.calls = []
    FakeAsyncClient.outcomes = [200]
    used_proxy = send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert used_proxy == "http://reserve:8888"
    assert FakeAsyncClient.calls == ["http://reserve:8888"]


def test_send_message_does_not_retry_payload_error(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [400])

    with raises(telegram.TelegramPayloadError):
        send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert FakeAsyncClient.calls == ["http://primary:8888"]


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_send_message_does_not_retry_permanent_error(
    monkeypatch: MonkeyPatch,
    status_code: int,
) -> None:
    setup_fake_client(monkeypatch, [status_code])

    with raises(telegram.TelegramPermanentError):
        send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert FakeAsyncClient.calls == ["http://primary:8888"]


def test_send_message_retries_5xx(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [502, 200])

    used_proxy = send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert used_proxy == "http://reserve:8888"
    assert FakeAsyncClient.calls == ["http://primary:8888", "http://reserve:8888"]


def test_send_message_retries_429_after_bounded_retry_after(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [(429, {"parameters": {"retry_after": 30}}), 200])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("alertsbot.telegram.asyncio.sleep", record_sleep)

    used_proxy = send_for_test(("http://primary:8888",))

    assert used_proxy == "http://primary:8888"
    assert FakeAsyncClient.calls == ["http://primary:8888", "http://primary:8888"]
    assert sleeps == [1.0]


def test_send_message_raises_429_after_retry_is_exhausted(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(
        monkeypatch,
        [
            (429, {"parameters": {"retry_after": 30}}),
            (429, {"parameters": {"retry_after": 30}}),
        ],
    )
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("alertsbot.telegram.asyncio.sleep", record_sleep)

    with raises(telegram.TelegramRateLimitError):
        send_for_test(("http://primary:8888",))

    assert FakeAsyncClient.calls == ["http://primary:8888", "http://primary:8888"]
    assert sleeps == [1.0]


def test_send_message_ignores_non_numeric_retry_after(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [(429, {"parameters": {"retry_after": "5"}}), 200])
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("alertsbot.telegram.asyncio.sleep", record_sleep)

    used_proxy = send_for_test(("http://primary:8888",))

    assert used_proxy == "http://primary:8888"
    assert FakeAsyncClient.calls == ["http://primary:8888", "http://primary:8888"]
    assert sleeps == []


def test_send_message_tries_unhealthy_proxy_as_last_resort(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [httpx.ConnectError("primary down"), 200])
    telegram._mark_proxy_unhealthy("http://reserve:8888", 60.0)

    used_proxy = send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert used_proxy == "http://reserve:8888"
    assert FakeAsyncClient.calls == ["http://primary:8888", "http://reserve:8888"]


def test_send_message_does_not_ban_proxy_after_telegram_5xx(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [502, 200])
    send_for_test(("http://primary:8888", "http://reserve:8888"))

    FakeAsyncClient.calls = []
    FakeAsyncClient.outcomes = [200]
    used_proxy = send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert used_proxy == "http://primary:8888"
    assert FakeAsyncClient.calls == ["http://primary:8888"]


def test_send_message_client_pool_keeps_proxy_fallback(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [httpx.ConnectError("primary down"), 200])
    client_pool = telegram.TelegramClientPool(timeout=1.0)

    try:
        used_proxy = send_for_test(
            ("http://primary:8888", "http://reserve:8888"),
            client_pool=client_pool,
        )
    finally:
        asyncio.run(client_pool.aclose())

    assert used_proxy == "http://reserve:8888"
    assert FakeAsyncClient.calls == ["http://primary:8888", "http://reserve:8888"]
    assert FakeAsyncClient.created_proxies == ["http://primary:8888", "http://reserve:8888"]


def test_send_message_logs_http_status_without_bot_token(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    secret_token = "123456:VERY_SECRET_TOKEN"
    setup_fake_client(monkeypatch, [502, 502])

    with caplog.at_level(logging.WARNING, logger="alertsbot.telegram"):
        with raises(httpx.HTTPStatusError):
            send_for_test(("http://primary:8888",), token=secret_token)

    assert secret_token not in caplog.text
    assert "HTTPStatusError status=502" in caplog.text
