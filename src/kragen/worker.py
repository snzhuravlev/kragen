"""Dedicated task worker process entrypoint."""

from __future__ import annotations

from kragen.logging_config import configure_logging
from kragen.config import get_settings
from kragen.services.task_queue import run_worker_process


def main() -> None:
    """Run the Redis-backed task worker loop."""
    settings = get_settings()
    configure_logging(settings.app.log_level)
    run_worker_process()


if __name__ == "__main__":
    main()
