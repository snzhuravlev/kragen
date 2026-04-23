"""Unit tests for combined single-service runner helpers."""

from __future__ import annotations

from kragen.cli.service_runner import _build_api_command, _build_telegram_command
from kragen.config import get_settings


def test_build_api_command() -> None:
    settings = get_settings()
    cmd = _build_api_command()
    assert cmd[1:4] == ["-m", "uvicorn", "kragen.api.main:app"]
    assert "--host" in cmd
    assert "--port" in cmd
    assert settings.api.host in cmd
    assert str(settings.api.port) in cmd


def test_build_telegram_command() -> None:
    cmd = _build_telegram_command()
    assert len(cmd) == 3
    assert cmd[1:] == ["-m", "kragen.channels.telegram_adapter"]
