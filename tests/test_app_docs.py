from __future__ import annotations

import importlib
import sys
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from alertsbot.config import get_settings


def load_app_with_env(monkeypatch: MonkeyPatch, alerts_env: str) -> FastAPI:
    monkeypatch.setenv("ALERTS_ENV", alerts_env)
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

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
