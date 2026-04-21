"""Конфигурация alertsbot."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки alertsbot из окружения."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    alerts_env: str = Field(default="dev", alias="ALERTS_ENV")
    alerts_bot_token: str = Field(default="", alias="ALERTS_BOT_TOKEN")
    alerts_chat_id: str = Field(default="", alias="ALERTS_CHAT_ID")
    alerts_token: str = Field(default="", alias="ALERTS_TOKEN")
    telegram_proxy_url: str = Field(default="", alias="TELEGRAM_PROXY_URL")
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


@lru_cache
def get_settings() -> Settings:
    """Возвращает кэшированные настройки."""

    return Settings()
