"""Background start/stop/status helpers for the Kragen HTTP API (PID file under .kragen/)."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

PID_FILENAME = "kragen-api.pid"
LOG_FILENAME = "kragen-api.log"


def repo_root() -> Path:
    """Repository root: .../src/kragen/cli/web_server_ctl.py -> parents[3]."""
    return Path(__file__).resolve().parents[3]


def state_dir() -> Path:
    d = repo_root() / ".kragen"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_path() -> Path:
    return state_dir() / PID_FILENAME


def log_path() -> Path:
    return state_dir() / LOG_FILENAME


def _read_pid_file() -> int | None:
    p = pid_path()
    if not p.is_file():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def _write_pid(pid: int) -> None:
    pid_path().write_text(str(pid), encoding="utf-8")


def _remove_pid_file() -> None:
    try:
        pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def _api_health_reachable(port: int, *, timeout: float = 1.5) -> bool:
    """True if something responds to GET /health on loopback (another instance may be running)."""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=timeout)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _print_port_help(port: int) -> None:
    print(
        f"  Hint: find what is bound to port {port}:\n"
        f"    ss -tlnp | grep ':{port}'\n"
        f"    or: lsof -i :{port}\n"
        "  If you started the API manually (uvicorn/kragen-api), stop that terminal or kill that PID.",
        file=sys.stderr,
    )


def _terminate_unix_process_group(pid: int, *, grace_s: float = 18.0) -> None:
    """
    Send SIGTERM/SIGKILL to the whole process group.

    `scripts/start.py` uses start_new_session=True, so the uvicorn process is usually the group
    leader; killpg catches any child (e.g. reloader) that shares the same PGID. A plain kill(pid)
    can leave children alive so the port stays open.
    """
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        if exc.errno in (errno.EPERM, errno.EINVAL):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as exc2:
                if exc2.errno != errno.ESRCH:
                    print(f"Failed to send SIGTERM to process: {exc2}", file=sys.stderr)
        else:
            print(f"Failed to send SIGTERM to process group: {exc}", file=sys.stderr)

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return
        time.sleep(0.2)

    if not is_pid_alive(pid):
        return

    print("Force-killing process group (SIGKILL)...", file=sys.stderr)
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def is_pid_alive(pid: int) -> bool:
    """Return True if process `pid` exists (best-effort)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creationflags,
        )
        out = (r.stdout or "").strip()
        if not out or "INFO:" in out.upper():
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def _http_base_for_probe(bind_host: str, port: int) -> str:
    """Use loopback for health checks when the server binds all interfaces."""
    if bind_host in ("0.0.0.0", "", "::", "[::]"):
        return f"http://127.0.0.1:{port}"
    if bind_host == "::1":
        return f"http://[::1]:{port}"
    return f"http://{bind_host}:{port}"


def cmd_start() -> int:
    """Start uvicorn in the background; write PID and append logs to .kragen/kragen-api.log."""
    existing = _read_pid_file()
    if existing is not None and is_pid_alive(existing):
        print(f"Already running (PID {existing}). Stop with: python scripts/stop.py")
        return 1

    if existing is not None:
        _remove_pid_file()

    from kragen.config import get_settings

    settings = get_settings()
    host = settings.api.host
    port = settings.api.port

    log_f = log_path().open("a", encoding="utf-8")
    log_f.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log_f.flush()

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "kragen.api.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        cmd,
        cwd=repo_root(),
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=sys.platform != "win32",
        creationflags=creationflags,
    )
    _write_pid(proc.pid)
    print(f"Started Kragen API, PID {proc.pid}")
    print(f"  URL: {_http_base_for_probe(host, port)}")
    print(f"  Log: {log_path()}")
    return 0


def cmd_stop() -> int:
    """Stop the process recorded in the PID file."""
    from kragen.config import get_settings

    port = int(get_settings().api.port)

    pid = _read_pid_file()
    if pid is None:
        if _api_health_reachable(port):
            print(
                "No PID file, but something on port "
                f"{port} still answers GET /health — not started via scripts/start.py, "
                "or the PID file was removed.",
                file=sys.stderr,
            )
            _print_port_help(port)
            return 2
        print("No PID file — server was not started via scripts/start.py or is already stopped.")
        return 1

    if not is_pid_alive(pid):
        print(f"Process {pid} not found; removing stale PID file.")
        _remove_pid_file()
        if _api_health_reachable(port):
            print(
                f"Warning: port {port} is still in use by another process (not PID {pid}).",
                file=sys.stderr,
            )
            _print_port_help(port)
            return 2
        return 0

    print(f"Stopping PID {pid} (Unix process group)...", flush=True)
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    else:
        _terminate_unix_process_group(pid)

    _remove_pid_file()

    # Give the kernel and uvicorn a moment to release the listening socket.
    time.sleep(0.5)

    if _api_health_reachable(port):
        print(
            "After signals, GET /health on port "
            f"{port} still succeeds — likely a second API instance "
            "(another terminal, systemd, docker, etc.).",
            file=sys.stderr,
        )
        _print_port_help(port)
        if os.environ.get("KRAGEN_STOP_USE_FUSER") == "1":
            print("KRAGEN_STOP_USE_FUSER=1 — trying fuser -k on port...", file=sys.stderr)
            subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                check=False,
                capture_output=True,
                text=True,
            )
            time.sleep(0.5)
            if not _api_health_reachable(port):
                print("Port cleared (fuser).")
                return 0
        return 2

    print("Stopped.")
    return 0


def cmd_status() -> int:
    """Print PID state and optionally GET /health."""
    pid = _read_pid_file()
    if pid is None:
        print("Status: not running (no PID file).")
        return 3

    if not is_pid_alive(pid):
        print(f"Status: not running (stale PID {pid} in {pid_path()})")
        return 3

    print(f"Status: running, PID {pid}")

    from kragen.config import get_settings

    settings = get_settings()
    base = _http_base_for_probe(settings.api.host, settings.api.port)
    health = f"{base.rstrip('/')}/health"
    try:
        r = httpx.get(health, timeout=30.0)
        print(f"  Health: {health} -> HTTP {r.status_code} {r.text.strip()[:200]}")
    except httpx.HTTPError as exc:
        print(f"  Health: unreachable ({exc})")
        return 2

    return 0
