"""Stdio MCP server: run OS commands inside task workspace."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kragen-os")
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_MAX_TIMEOUT_SECONDS = 300
_DEFAULT_OUTPUT_LIMIT = 20_000
_DEFAULT_SECURITY_PROFILE = "open_dev"
_DENY_SUBSTRINGS = (
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


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _security_profile() -> str:
    raw = os.environ.get("KRAGEN_OS_SECURITY_PROFILE", "").strip().lower()
    return raw or _DEFAULT_SECURITY_PROFILE


def _max_timeout_seconds() -> int:
    return max(1, _int_env("KRAGEN_OS_MAX_TIMEOUT_SECONDS", _DEFAULT_MAX_TIMEOUT_SECONDS))


def _output_limit() -> int:
    return max(1_000, _int_env("KRAGEN_OS_MAX_OUTPUT_BYTES", _DEFAULT_OUTPUT_LIMIT))


def _allowed_prefixes() -> list[str]:
    return _csv_env("KRAGEN_OS_ALLOWED_COMMAND_PREFIXES")


def _allowed_env_prefixes() -> list[str]:
    # open_dev default keeps behavior convenient while still explicit.
    values = _csv_env("KRAGEN_OS_ALLOWED_ENV_PREFIXES")
    if values:
        return values
    return ["KRAGEN_", "PYTHON", "PATH", "HOME", "LANG", "LC_"]


def _truncate_tail(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def _coerce_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _policy_block_reason(command_text: str, mode: str) -> str | None:
    text = command_text.strip().lower()
    if not text:
        return "command must not be empty"

    if mode in {"balanced", "strict"}:
        for marker in _DENY_SUBSTRINGS:
            if marker in text:
                return f"command contains denied pattern: {marker!r}"

    if mode in {"balanced", "strict"}:
        prefixes = _allowed_prefixes()
        if prefixes and not any(text.startswith(prefix.lower()) for prefix in prefixes):
            return "command is not in allowlist prefixes: " + ", ".join(prefixes)

    if mode == "strict":
        if any(op in command_text for op in ("|", "&&", "||", ";", ">", "<", "`", "$(")):
            return "shell operators are not allowed in strict profile"
    return None


def _tool_env(extra_env: dict[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    if not extra_env:
        return env
    prefixes = _allowed_env_prefixes()
    for key, value in extra_env.items():
        if any(key.startswith(prefix) for prefix in prefixes):
            env[key] = value
    return env


def _run(command: list[str], cwd: Path, timeout_seconds: int, env: dict[str, str]) -> str:
    started = time.monotonic()
    timeout = max(1, min(int(timeout_seconds), _max_timeout_seconds()))
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        timed_out = False
        exit_code: int | None = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = _coerce_text(exc.stdout)
        stderr = _coerce_text(exc.stderr)

    duration_ms = int((time.monotonic() - started) * 1000)
    limit = _output_limit()
    stdout_out, stdout_trunc = _truncate_tail(stdout, limit)
    stderr_out, stderr_trunc = _truncate_tail(stderr, limit)
    payload = {
        "ok": (exit_code == 0) and not timed_out,
        "exit_code": exit_code,
        "stdout": stdout_out,
        "stderr": stderr_out,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "truncated": stdout_trunc or stderr_trunc,
        "cwd": str(cwd),
        "command": " ".join(command),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def run_process(
    command: str,
    cwd: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> str:
    """Run an OS command in task workspace with security-profile guardrails."""
    try:
        command_text = command.strip()
        mode = _security_profile()
        reason = _policy_block_reason(command_text, mode)
        if reason is not None:
            return f"error: blocked by policy: {reason}"
        argv = shlex.split(command_text)
        if not argv:
            return "error: command must not be empty"
        target = _resolve_cwd(cwd)
        return _run(argv, target, timeout_seconds, _tool_env(env))
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}"


@mcp.tool()
def run_shell(
    shell: str,
    script: str,
    cwd: str | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> str:
    """Run shell script text (bash/sh/powershell/pwsh) in task workspace."""
    try:
        shell_name = shell.strip().lower()
        if shell_name not in {"bash", "sh", "powershell", "pwsh"}:
            return "error: shell must be one of: bash, sh, powershell, pwsh"
        mode = _security_profile()
        reason = _policy_block_reason(script, mode)
        if reason is not None:
            return f"error: blocked by policy: {reason}"
        target = _resolve_cwd(cwd)
        if shell_name in {"bash", "sh"}:
            cmd = [shell_name, "-lc", script]
        else:
            cmd = [shell_name, "-Command", script]
        return _run(cmd, target, timeout_seconds, _tool_env(env))
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}: {exc}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
