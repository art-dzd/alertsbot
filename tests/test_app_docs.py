from __future__ import annotations

import importlib
import logging
import sys
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from alertsbot.config import get_settings


def access_record(path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", path, "1.1", status),
        None,
    )


def load_app_with_env(monkeypatch: MonkeyPatch, alerts_env: str) -> FastAPI:
    monkeypatch.setenv("ALERTS_ENV", alerts_env)
    monkeypatch.setenv("ALERTS_BOT_TOKEN", "123456:bot-token")
    monkeypatch.setenv("ALERTS_CHAT_ID", "-100")
    monkeypatch.setenv("ALERTS_TOKEN", "shared-secret")
    get_settings.cache_clear()
    sys.modules.pop("alertsbot.app", None)
    return cast(FastAPI, importlib.import_module("alertsbot.app").app)


def test_openapi_docs_are_disabled_in_prod(monkeypatch: MonkeyPatch) -> None:
    client = TestClient(load_app_with_env(monkeypatch, "prod"))

    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_openapi_docs_are_enabled_outside_prod(monkeypatch: MonkeyPatch) -> None:
    client = TestClient(load_app_with_env(monkeypatch, "dev"))

    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_health_endpoint_stays_available_in_prod(monkeypatch: MonkeyPatch) -> None:
    client = TestClient(load_app_with_env(monkeypatch, "production"))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_endpoint_stays_available_in_prod(monkeypatch: MonkeyPatch) -> None:
    client = TestClient(load_app_with_env(monkeypatch, "production"))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_endpoint_reports_ready_with_valid_config(monkeypatch: MonkeyPatch) -> None:
    client = TestClient(load_app_with_env(monkeypatch, "production"))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_health_access_filter_hides_only_success(monkeypatch: MonkeyPatch) -> None:
    load_app_with_env(monkeypatch, "production")
    access_filter = logging.getLogger("uvicorn.access").filters[-1]

    assert access_filter.filter(access_record("/healthz", 200)) is False
    assert access_filter.filter(access_record("/readyz", 503)) is True
    assert access_filter.filter(access_record("/notify", 200)) is True
