"""Dedicated task worker process entrypoint."""

from __future__ import annotations

from kragen.logging_config import configure_logging
from kragen.config import get_settings
from kragen.plugins.manager import bootstrap_plugins
from kragen.services.task_queue import run_worker_process
from kragen.services import task_stream


def main() -> None:
    """Run the Redis-backed task worker loop."""
    settings = get_settings()
    configure_logging(settings.app.log_level)
    task_stream.configure_from_settings()
    bootstrap_plugins()
    run_worker_process()


if __name__ == "__main__":
    main()
