"""Клиент Telegram для отправки сообщений."""

from __future__ import annotations

import httpx


async def send_message(token: str, chat_id: str, text: str, timeout: float) -> None:
    """Отправляет сообщение в Telegram."""

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
