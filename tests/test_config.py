from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from alertsbot.config import Settings


def prod_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ALERTS_ENV": "prod",
        "ALERTS_BOT_TOKEN": "123456:bot-token",
        "ALERTS_CHAT_ID": "-100",
        "ALERTS_TOKEN": "shared-secret",
    }
    values.update(overrides)
    return Settings(**values)


def test_dev_allows_empty_alert_secrets() -> None:
    settings = Settings()

    assert settings.is_production is False


@pytest.mark.parametrize(
    ("field_name", "env_name"),
    [
        ("ALERTS_BOT_TOKEN", "ALERTS_BOT_TOKEN"),
        ("ALERTS_CHAT_ID", "ALERTS_CHAT_ID"),
        ("ALERTS_TOKEN", "ALERTS_TOKEN"),
    ],
)
def test_prod_rejects_empty_required_secrets(field_name: str, env_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name):
        prod_settings(**{env_name: ""})


@pytest.mark.parametrize(
    ("field_name", "env_name"),
    [
        ("ALERTS_BOT_TOKEN", "ALERTS_BOT_TOKEN"),
        ("ALERTS_CHAT_ID", "ALERTS_CHAT_ID"),
        ("ALERTS_TOKEN", "ALERTS_TOKEN"),
    ],
)
def test_prod_rejects_placeholder_required_secrets(field_name: str, env_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name):
        prod_settings(**{env_name: "replace_me"})


def test_prod_accepts_real_required_secrets() -> None:
    settings = prod_settings()

    assert settings.is_production is True


def test_request_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="ALERTS_REQUEST_TIMEOUT_SECONDS"):
        prod_settings(ALERTS_REQUEST_TIMEOUT_SECONDS=0)


def test_proxy_circuit_breaker_seconds_cannot_be_negative() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_PROXY_CIRCUIT_BREAKER_SECONDS"):
        prod_settings(TELEGRAM_PROXY_CIRCUIT_BREAKER_SECONDS=-1)
