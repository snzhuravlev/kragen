"""Tests for kragen-os MCP helpers."""

from __future__ import annotations

import json

from kragen.mcp import kragen_os_mcp as os_mcp


def test_run_process_in_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    out = os_mcp.run_process("python -c \"print('ok')\"", cwd=".", timeout_seconds=5)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "ok" in payload["stdout"]


def test_run_process_rejects_cwd_escape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    out = os_mcp.run_process("python -V", cwd="../", timeout_seconds=5)
    assert out.startswith("error:")
    assert "cwd escapes" in out


def test_run_process_timeout_sets_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    out = os_mcp.run_process(
        "python -c \"import time; time.sleep(2)\"",
        cwd=".",
        timeout_seconds=1,
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["timed_out"] is True
    assert payload["exit_code"] is None


def test_run_process_truncates_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("KRAGEN_OS_MAX_OUTPUT_BYTES", "1200")
    out = os_mcp.run_process(
        "python -c \"print('x' * 5000)\"",
        cwd=".",
        timeout_seconds=5,
    )
    payload = json.loads(out)
    assert payload["truncated"] is True
    assert len(payload["stdout"]) <= 1200


def test_run_process_env_prefix_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("KRAGEN_OS_ALLOWED_ENV_PREFIXES", "SAFE_")
    out = os_mcp.run_process(
        "python -c \"import os; print(os.getenv('SAFE_TOKEN')); print(os.getenv('BAD_TOKEN'))\"",
        cwd=".",
        timeout_seconds=5,
        env={"SAFE_TOKEN": "ok", "BAD_TOKEN": "blocked"},
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "ok" in payload["stdout"]
    assert "blocked" not in payload["stdout"]
