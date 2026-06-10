from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from pytest import LogCaptureFixture, MonkeyPatch, raises

from alertsbot import telegram
from alertsbot.config import Settings


class FakeResponse:
    def __init__(self, status_code: int, url: str) -> None:
        request = httpx.Request("POST", url)
        self.response = httpx.Response(status_code, request=request)

    def raise_for_status(self) -> None:
        self.response.raise_for_status()


class FakeAsyncClient:
    calls: list[str] = []
    outcomes: list[Exception | int] = []

    def __init__(
        self,
        *,
        timeout: float,
        trust_env: bool,
        proxy: str = "",
    ) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.trust_env = trust_env

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        self.calls.append(self.proxy)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        return FakeResponse(outcome, url)


def setup_fake_client(monkeypatch: MonkeyPatch, outcomes: list[Exception | int]) -> None:
    telegram.reset_proxy_circuit_breakers()
    FakeAsyncClient.calls = []
    FakeAsyncClient.outcomes = outcomes
    monkeypatch.setattr("alertsbot.telegram.httpx.AsyncClient", FakeAsyncClient)


def send_for_test(proxy_urls: tuple[str, ...], token: str = "token") -> str:
    return asyncio.run(telegram.send_message(token, "chat", "text", 1.0, proxy_urls, 60.0))


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


def test_send_message_does_not_retry_telegram_4xx(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [401])

    with raises(httpx.HTTPStatusError):
        send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert FakeAsyncClient.calls == ["http://primary:8888"]


def test_send_message_retries_5xx(monkeypatch: MonkeyPatch) -> None:
    setup_fake_client(monkeypatch, [502, 200])

    used_proxy = send_for_test(("http://primary:8888", "http://reserve:8888"))

    assert used_proxy == "http://reserve:8888"
    assert FakeAsyncClient.calls == ["http://primary:8888", "http://reserve:8888"]


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
