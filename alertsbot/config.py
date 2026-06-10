"""Конфигурация alertsbot."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_VALUES = {"", "replace_me", "replace-me", "changeme", "change_me"}


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDER_VALUES


class Settings(BaseSettings):
    """Настройки alertsbot из окружения."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    alerts_env: str = Field(default="dev", alias="ALERTS_ENV")
    alerts_bot_token: str = Field(default="", alias="ALERTS_BOT_TOKEN")
    alerts_chat_id: str = Field(default="", alias="ALERTS_CHAT_ID")
    alerts_token: str = Field(default="", alias="ALERTS_TOKEN")
    telegram_proxy_url: str = Field(default="", alias="TELEGRAM_PROXY_URL")
    telegram_proxy_urls: str = Field(default="", alias="TELEGRAM_PROXY_URLS")
    telegram_proxy_circuit_breaker_seconds: float = Field(
        default=120.0,
        alias="TELEGRAM_PROXY_CIRCUIT_BREAKER_SECONDS",
    )
    app_host: str = Field(default="0.0.0.0", alias="ALERTS_APP_HOST")
    app_port: int = Field(default=9100, alias="ALERTS_APP_PORT")
    log_level: str = Field(default="INFO", alias="ALERTS_LOG_LEVEL")
    request_timeout_seconds: float = Field(
        default=10.0,
        alias="ALERTS_REQUEST_TIMEOUT_SECONDS",
    )

    @property
    def is_production(self) -> bool:
        """Возвращает true для production-окружения."""

        return self.alerts_env.strip().lower() in {"prod", "production"}

    @property
    def telegram_proxy_sequence(self) -> tuple[str, ...]:
        """Возвращает список Telegram proxy с обратной совместимостью."""

        proxy_urls = tuple(
            proxy_url.strip()
            for proxy_url in self.telegram_proxy_urls.split(",")
            if proxy_url.strip()
        )
        if proxy_urls:
            return proxy_urls

        proxy_url = self.telegram_proxy_url.strip()
        if proxy_url:
            return (proxy_url,)

        return ("",)

    @model_validator(mode="after")
    def validate_runtime_settings(self) -> Settings:
        self._validate_timing()
        if self.is_production:
            self._validate_prod_secret("ALERTS_BOT_TOKEN", self.alerts_bot_token)
            self._validate_prod_secret("ALERTS_CHAT_ID", self.alerts_chat_id)
            self._validate_prod_secret("ALERTS_TOKEN", self.alerts_token)

        return self

    def _validate_timing(self) -> None:
        if self.request_timeout_seconds <= 0:
            raise ValueError("ALERTS_REQUEST_TIMEOUT_SECONDS must be greater than 0")

        if self.telegram_proxy_circuit_breaker_seconds < 0:
            raise ValueError("TELEGRAM_PROXY_CIRCUIT_BREAKER_SECONDS must be 0 or greater")

    @staticmethod
    def _validate_prod_secret(name: str, value: str) -> None:
        if _is_placeholder(value):
            raise ValueError(f"{name} must be set in production")


@lru_cache
def get_settings() -> Settings:
    """Возвращает кэшированные настройки."""

    return Settings()
