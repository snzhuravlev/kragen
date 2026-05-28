"""Tests for worker process plugin bootstrap."""

from __future__ import annotations

from unittest.mock import patch

from kragen.plugins.manager import bootstrap_plugins, reset_plugin_manager_for_tests


def test_bootstrap_plugins_initializes_manager() -> None:
    reset_plugin_manager_for_tests()
    manager = bootstrap_plugins()
    assert manager._initialized is True


def test_worker_main_bootstraps_plugins_before_loop() -> None:
    calls: list[str] = []

    def _bootstrap() -> object:
        calls.append("bootstrap")
        return bootstrap_plugins()

    with (
        patch("kragen.worker.bootstrap_plugins", side_effect=_bootstrap),
        patch("kragen.worker.run_worker_process") as run_loop,
        patch("kragen.worker.task_stream.configure_from_settings"),
        patch("kragen.worker.get_settings") as get_settings,
    ):
        get_settings.return_value.app.log_level = "INFO"
        from kragen.worker import main

        main()

    assert calls == ["bootstrap"]
    run_loop.assert_called_once()
