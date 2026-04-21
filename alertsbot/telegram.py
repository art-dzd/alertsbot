"""Клиент Telegram для отправки сообщений."""

from __future__ import annotations

import httpx


async def send_message(
    token: str,
    chat_id: str,
    text: str,
    timeout: float,
    proxy_url: str,
) -> None:
    """Отправляет сообщение в Telegram."""

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if proxy_url:
        client = httpx.AsyncClient(timeout=timeout, trust_env=False, proxy=proxy_url)
    else:
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)

    async with client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
