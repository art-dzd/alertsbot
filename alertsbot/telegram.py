"""Клиент Telegram для отправки сообщений."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)
_FINAL_PROXY_ATTEMPTS = 2
_RATE_LIMIT_RETRY_ATTEMPTS = 1
_MAX_RATE_LIMIT_SLEEP_SECONDS = 10.0
_MAX_CONNECTIONS = 20
_MAX_KEEPALIVE_CONNECTIONS = 10

_unhealthy_proxy_until: dict[str, float] = {}


class TelegramError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class TelegramPayloadError(TelegramError):
    pass


class TelegramPermanentError(TelegramError):
    pass


class TelegramRateLimitError(TelegramError):
    pass


class TelegramClientPool:
    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._limits = httpx.Limits(
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
        )
        self._clients: dict[str, httpx.AsyncClient] = {}

    def client_for(self, proxy_url: str) -> httpx.AsyncClient:
        normalized_proxy_url = proxy_url.strip()
        client = self._clients.get(normalized_proxy_url)
        if client is None:
            client = _create_client(self._timeout, normalized_proxy_url, self._limits)
            self._clients[normalized_proxy_url] = client

        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

        self._clients.clear()


def reset_proxy_circuit_breakers() -> None:
    """Сбрасывает состояние circuit breaker для тестов и ручной диагностики."""

    _unhealthy_proxy_until.clear()


def describe_proxy(proxy_url: str) -> str:
    """Возвращает безопасное имя proxy без логирования credentials."""

    if not proxy_url:
        return "direct"

    parsed = urlsplit(proxy_url)
    if not parsed.hostname:
        return "configured-proxy"

    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _ordered_proxy_urls(proxy_urls: Sequence[str]) -> tuple[str, ...]:
    candidates = tuple(dict.fromkeys(proxy_url.strip() for proxy_url in proxy_urls)) or ("",)
    now = time.monotonic()

    healthy = tuple(
        proxy_url for proxy_url in candidates if _unhealthy_proxy_until.get(proxy_url, 0.0) <= now
    )
    unhealthy = tuple(proxy_url for proxy_url in candidates if proxy_url not in healthy)
    return healthy + unhealthy


def _mark_proxy_unhealthy(proxy_url: str, retry_after_seconds: float) -> None:
    if retry_after_seconds <= 0:
        return

    _unhealthy_proxy_until[proxy_url] = time.monotonic() + retry_after_seconds


def _mark_proxy_healthy(proxy_url: str) -> None:
    _unhealthy_proxy_until.pop(proxy_url, None)


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 500

    return isinstance(error, httpx.TimeoutException | httpx.TransportError)


def _should_mark_proxy_unhealthy(error: Exception) -> bool:
    return isinstance(error, httpx.TimeoutException | httpx.TransportError)


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        data: Any = response.json()
    except ValueError:
        return 0.0

    if not isinstance(data, dict):
        return 0.0

    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        return 0.0

    retry_after = parameters.get("retry_after")
    if isinstance(retry_after, int | float):
        return max(float(retry_after), 0.0)

    return 0.0


def _bounded_rate_limit_sleep_seconds(retry_after_seconds: float | None) -> float:
    if retry_after_seconds is None:
        return 0.0

    return min(retry_after_seconds, _MAX_RATE_LIMIT_SLEEP_SECONDS)


def _raise_for_telegram_status(response: httpx.Response) -> None:
    status_code = response.status_code
    if status_code == 429:
        retry_after_seconds = _retry_after_seconds(response)
        raise TelegramRateLimitError(
            "Telegram rate limit",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
        )

    if status_code == 400:
        raise TelegramPayloadError("Telegram rejected payload", status_code=status_code)

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        if 400 <= status_code < 500:
            raise TelegramPermanentError(
                "Telegram permanent error",
                status_code=status_code,
            ) from error

        raise


def _create_client(
    timeout: float,
    proxy_url: str,
    limits: httpx.Limits | None = None,
) -> httpx.AsyncClient:
    client_kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": False}
    if limits is not None:
        client_kwargs["limits"] = limits
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    return httpx.AsyncClient(**client_kwargs)


async def _send_message_once(
    token: str,
    chat_id: str,
    text: str,
    proxy_url: str,
    client_pool: TelegramClientPool,
    stats: DeliveryStats,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    client = client_pool.client_for(proxy_url)
    response = await client.post(url, json=payload)
    stats.status = response.status_code
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = None
        error_code = body.get("error_code") if isinstance(body, dict) else None
        stats.error_code = error_code if isinstance(error_code, int) else response.status_code
    _raise_for_telegram_status(response)


@dataclass(slots=True)
class DeliveryStats:
    attempts: int = 0
    proxy: str | None = None
    status: int | None = None
    error_code: int | None = None
    telegram_ms: float = 0.0
    cached: bool = False
    exception: Exception | None = None


async def send_message(
    token: str,
    chat_id: str,
    text: str,
    proxy_urls: Sequence[str],
    circuit_breaker_seconds: float,
    *,
    client_pool: TelegramClientPool,
    stats: DeliveryStats,
) -> str:
    """Отправляет через обязательный пул, сохраняя метаданные всех попыток."""

    last_retryable_error: Exception | None = None
    candidates = _ordered_proxy_urls(proxy_urls)
    rate_limit_retries_left = _RATE_LIMIT_RETRY_ATTEMPTS

    for proxy_index, proxy_url in enumerate(candidates):
        attempts = _FINAL_PROXY_ATTEMPTS if proxy_index == len(candidates) - 1 else 1
        attempt = 1
        while attempt <= attempts:
            started = time.monotonic()
            stats.attempts += 1
            stats.proxy = describe_proxy(proxy_url)
            stats.status = None
            stats.error_code = None
            try:
                await _send_message_once(token, chat_id, text, proxy_url, client_pool, stats)
            except Exception as error:
                sleep_seconds = 0.0
                if isinstance(error, TelegramRateLimitError):
                    if rate_limit_retries_left <= 0:
                        raise
                    rate_limit_retries_left -= 1
                    sleep_seconds = _bounded_rate_limit_sleep_seconds(error.retry_after_seconds)
                elif _is_retryable_error(error):
                    last_retryable_error = error
                    if attempt >= attempts and _should_mark_proxy_unhealthy(error):
                        _mark_proxy_unhealthy(proxy_url, circuit_breaker_seconds)
                    if attempt >= attempts and proxy_index == len(candidates) - 1:
                        raise
                    attempt += 1
                else:
                    raise

                logger.warning(
                    "Повтор отправки в Telegram",
                    extra={
                        "event": "telegram_retry",
                        "fields": {
                            "reason": type(error).__name__,
                            "attempt": stats.attempts,
                            "duration_ms": round((time.monotonic() - started) * 1000, 3),
                            "status": stats.status,
                            "error_code": stats.error_code,
                            "proxy": stats.proxy,
                            "retry_after_seconds": sleep_seconds,
                        },
                    },
                )
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                continue

            _mark_proxy_healthy(proxy_url)
            return proxy_url

    if last_retryable_error:
        raise last_retryable_error
    raise RuntimeError("No Telegram proxy candidates configured")
