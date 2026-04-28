"""Stdio MCP server: run bash/python scripts inside task workspace."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kragen-scripts")
_DEFAULT_TIMEOUT_SECONDS = 60
_MAX_TIMEOUT_SECONDS = 300
_OUTPUT_LIMIT = 20000
_DEFAULT_ALLOWLIST = ("python", "python3", "bash", "sh", "ls", "pwd", "cat", "rg", "uv", "pytest")
_DEFAULT_DENY_SUBSTRINGS = (
    "rm -rf /",
    "mkfs",
    "shutdown",
    "reboot",
    ":(){:|:&};:",
    "dd if=/dev/zero",
)


def _workspace_root() -> Path:
    root = os.environ.get("KRAGEN_TASK_WORKSPACE_DIR", "").strip()
    if not root:
        raise RuntimeError("KRAGEN_TASK_WORKSPACE_DIR is not set")
    path = Path(root).resolve()
    if not path.is_dir():
        raise RuntimeError(f"Workspace dir does not exist: {path}")
    return path


def _resolve_cwd(cwd: str | None) -> Path:
    root = _workspace_root()
    rel = cwd.strip() if cwd else "."
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("cwd escapes KRAGEN_TASK_WORKSPACE_DIR") from exc
    if not target.is_dir():
        raise RuntimeError(f"cwd is not a directory: {target}")
    return target


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _allowlist() -> list[str]:
    return _split_csv(os.environ.get("KRAGEN_SCRIPTS_ALLOWED_PREFIXES")) or list(_DEFAULT_ALLOWLIST)


def _deny_substrings() -> list[str]:
    return _split_csv(os.environ.get("KRAGEN_SCRIPTS_DENY_SUBSTRINGS")) or list(_DEFAULT_DENY_SUBSTRINGS)


def _is_run_command_enabled() -> bool:
    raw = (os.environ.get("KRAGEN_SCRIPTS_ENABLE_RUN_COMMAND") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _validate_command_text(command_text: str) -> str | None:
    command_lower = command_text.lower()
    for marker in _deny_substrings():
        if marker.lower() in command_lower:
            return f"command contains denied pattern: {marker!r}"
    prefixes = _allowlist()
    if not any(command_lower.startswith(prefix.lower()) for prefix in prefixes):
        return (
            "command is not in allowlist prefixes: "
            + ", ".join(prefixes)
        )
    return None


def _run(
    *,
    command: list[str],
    cwd: str | None,
    timeout_seconds: int,
) -> str:
    target = _resolve_cwd(cwd)
    timeout = max(1, min(int(timeout_seconds), _MAX_TIMEOUT_SECONDS))
    proc = subprocess.run(
        command,
        cwd=str(target),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
        check=False,
    )
    payload = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "cwd": str(target),
        "command": command,
        "stdout": proc.stdout[-_OUTPUT_LIMIT:],
        "stderr": proc.stderr[-_OUTPUT_LIMIT:],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def run_bash(
    script: str,
    cwd: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run bash script text inside KRAGEN_TASK_WORKSPACE_DIR."""
    if not script.strip():
        return "error: script must not be empty"
    try:
        return _run(
            command=["bash", "-lc", script],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}"


@mcp.tool()
def run_python(
    code: str,
    cwd: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Run inline Python code inside KRAGEN_TASK_WORKSPACE_DIR."""
    if not code.strip():
        return "error: code must not be empty"
    try:
        return _run(
            command=["python", "-c", code],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}"


@mcp.tool()
def run_command(
    command: str,
    shell: str = "bash",
    cwd: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """
    Run one command under safety policy.

    Policy:
    - Disabled unless KRAGEN_SCRIPTS_ENABLE_RUN_COMMAND=true.
    - Command must match allowlist prefixes (KRAGEN_SCRIPTS_ALLOWED_PREFIXES).
    - Command must not contain deny substrings (KRAGEN_SCRIPTS_DENY_SUBSTRINGS).
    """
    if not _is_run_command_enabled():
        return (
            "error: run_command is disabled by policy "
            "(set KRAGEN_SCRIPTS_ENABLE_RUN_COMMAND=true to enable)"
        )
    command_text = command.strip()
    if not command_text:
        return "error: command must not be empty"
    reason = _validate_command_text(command_text)
    if reason is not None:
        return f"error: blocked by policy: {reason}"
    shell_name = shell.strip().lower()
    try:
        if shell_name in ("bash", "sh"):
            return _run(
                command=[shell_name, "-lc", command_text],
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            )
        if shell_name in ("none", "exec"):
            argv = shlex.split(command_text)
            if not argv:
                return "error: command must not be empty"
            return _run(command=argv, cwd=cwd, timeout_seconds=timeout_seconds)
        return "error: shell must be one of: bash, sh, none"
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
