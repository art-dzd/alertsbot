"""Клиент Telegram для отправки сообщений."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)
_FINAL_PROXY_ATTEMPTS = 2

_unhealthy_proxy_until: dict[str, float] = {}


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


def describe_telegram_error(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"{type(error).__name__} status={error.response.status_code}"

    return type(error).__name__


def _ordered_proxy_urls(proxy_urls: Sequence[str]) -> tuple[str, ...]:
    candidates = tuple(dict.fromkeys(proxy_url.strip() for proxy_url in proxy_urls)) or ("",)
    now = time.monotonic()

    healthy = tuple(
        proxy_url
        for proxy_url in candidates
        if _unhealthy_proxy_until.get(proxy_url, 0.0) <= now
    )
    if healthy:
        return healthy

    return candidates


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


def _create_client(timeout: float, proxy_url: str) -> httpx.AsyncClient:
    if proxy_url:
        return httpx.AsyncClient(timeout=timeout, trust_env=False, proxy=proxy_url)

    return httpx.AsyncClient(timeout=timeout, trust_env=False)


async def _send_message_once(
    token: str,
    chat_id: str,
    text: str,
    timeout: float,
    proxy_url: str,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    async with _create_client(timeout, proxy_url) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()


async def send_message(
    token: str,
    chat_id: str,
    text: str,
    timeout: float,
    proxy_urls: Sequence[str],
    circuit_breaker_seconds: float,
) -> str:
    """Отправляет сообщение в Telegram, перебирая proxy при сетевых сбоях."""

    last_retryable_error: Exception | None = None
    candidates = _ordered_proxy_urls(proxy_urls)

    for proxy_index, proxy_url in enumerate(candidates):
        attempts = _FINAL_PROXY_ATTEMPTS if proxy_index == len(candidates) - 1 else 1
        for attempt in range(1, attempts + 1):
            try:
                await _send_message_once(token, chat_id, text, timeout, proxy_url)
            except Exception as error:
                if not _is_retryable_error(error):
                    raise

                last_retryable_error = error
                if attempt < attempts:
                    logger.warning(
                        "Telegram send failed via %s, retrying final proxy: %s",
                        describe_proxy(proxy_url),
                        describe_telegram_error(error),
                    )
                    continue

                _mark_proxy_unhealthy(proxy_url, circuit_breaker_seconds)
                logger.warning(
                    "Telegram send failed via %s: %s",
                    describe_proxy(proxy_url),
                    describe_telegram_error(error),
                )
                continue

            _mark_proxy_healthy(proxy_url)
            return proxy_url

    if last_retryable_error:
        raise last_retryable_error

    raise RuntimeError("No Telegram proxy candidates configured")
