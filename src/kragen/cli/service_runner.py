"""Run Kragen API and Telegram channel under a single service process."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from kragen.config import get_settings
from kragen.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


def _repo_root() -> Path:
    """Best-effort repository root path for subprocess working directory."""
    return Path(__file__).resolve().parents[3]


def _build_api_command() -> list[str]:
    """Build uvicorn command from Kragen API settings."""
    settings = get_settings()
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "kragen.api.main:app",
        "--host",
        settings.api.host,
        "--port",
        str(settings.api.port),
    ]


def _build_telegram_command() -> list[str]:
    """Build Telegram adapter command."""
    return [sys.executable, "-m", "kragen.channels.telegram_adapter"]


async def _stream_subprocess_logs(
    name: str,
    stream: asyncio.StreamReader,
) -> None:
    """Prefix child process logs with service name."""
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip()
        logger.info("service_child_log", service=name, line=text)


async def _terminate_process(proc: asyncio.subprocess.Process, *, name: str) -> None:
    """Gracefully terminate subprocess, escalate to kill if needed."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=20.0)
    except TimeoutError:
        logger.warning("service_child_force_kill", service=name)
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


async def _run_combined_service() -> int:
    """Run API and Telegram adapter together; return exit code."""
    env = os.environ.copy()
    cwd = str(_repo_root())
    api_cmd = _build_api_command()
    telegram_cmd = _build_telegram_command()

    api_proc = await asyncio.create_subprocess_exec(
        *api_cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    telegram_proc = await asyncio.create_subprocess_exec(
        *telegram_cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    assert api_proc.stdout is not None
    assert telegram_proc.stdout is not None

    api_log_task = asyncio.create_task(_stream_subprocess_logs("api", api_proc.stdout))
    telegram_log_task = asyncio.create_task(
        _stream_subprocess_logs("telegram", telegram_proc.stdout)
    )

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    wait_api = asyncio.create_task(api_proc.wait())
    wait_telegram = asyncio.create_task(telegram_proc.wait())
    wait_stop = asyncio.create_task(stop_event.wait())

    done, _pending = await asyncio.wait(
        {wait_api, wait_telegram, wait_stop},
        return_when=asyncio.FIRST_COMPLETED,
    )

    reason = "signal"
    if wait_api in done:
        reason = "api_exited"
    elif wait_telegram in done:
        reason = "telegram_exited"
    logger.warning("combined_service_stopping", reason=reason)

    await _terminate_process(api_proc, name="api")
    await _terminate_process(telegram_proc, name="telegram")

    for task in (api_log_task, telegram_log_task, wait_api, wait_telegram, wait_stop):
        if not task.done():
            task.cancel()
    try:
        await asyncio.gather(
            api_log_task,
            telegram_log_task,
            wait_api,
            wait_telegram,
            wait_stop,
            return_exceptions=True,
        )
    except Exception:  # noqa: BLE001
        pass

    api_rc = api_proc.returncode if api_proc.returncode is not None else 1
    telegram_rc = telegram_proc.returncode if telegram_proc.returncode is not None else 1
    if reason == "signal":
        return 0
    if api_rc == 0 and telegram_rc == 0:
        return 0
    return api_rc if api_rc != 0 else telegram_rc


def main() -> None:
    """Console entrypoint for single-service combined start."""
    settings = get_settings()
    configure_logging(settings.app.log_level)
    logger.info(
        "combined_service_start",
        api_host=settings.api.host,
        api_port=settings.api.port,
        telegram_mode=settings.telegram_channel.mode,
    )
    code = asyncio.run(_run_combined_service())
    if code != 0:
        raise SystemExit(code)
