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
    client_kwargs: dict[str, object] = {"timeout": timeout, "trust_env": False}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
