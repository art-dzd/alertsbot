from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from alertsbot.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    for name in os.environ:
        if name.startswith(("ALERTS_", "TELEGRAM_PROXY_")):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("ALERTS_DB_PATH", str(tmp_path / "alerts.db"))
    get_settings.cache_clear()
