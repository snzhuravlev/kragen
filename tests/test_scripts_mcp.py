"""Tests for kragen-scripts MCP helpers."""

from __future__ import annotations

import json

from kragen.mcp import kragen_scripts_mcp as scripts


def test_run_python_in_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    out = scripts.run_python("print('ok')", cwd=".", timeout_seconds=5)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "ok" in payload["stdout"]


def test_run_bash_rejects_cwd_escape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    out = scripts.run_bash("pwd", cwd="../", timeout_seconds=5)
    assert out.startswith("error:")


def test_run_command_disabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("KRAGEN_SCRIPTS_ENABLE_RUN_COMMAND", raising=False)
    out = scripts.run_command("python -V", shell="bash", cwd=".", timeout_seconds=5)
    assert "disabled by policy" in out


def test_run_command_enabled_and_allowed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("KRAGEN_SCRIPTS_ENABLE_RUN_COMMAND", "true")
    monkeypatch.setenv("KRAGEN_SCRIPTS_ALLOWED_PREFIXES", "python,ls")
    out = scripts.run_command("python -c \"print('ok')\"", shell="bash", cwd=".", timeout_seconds=5)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "ok" in payload["stdout"]


def test_run_command_blocked_by_deny_pattern(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KRAGEN_TASK_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("KRAGEN_SCRIPTS_ENABLE_RUN_COMMAND", "true")
    monkeypatch.setenv("KRAGEN_SCRIPTS_ALLOWED_PREFIXES", "python")
    monkeypatch.setenv("KRAGEN_SCRIPTS_DENY_SUBSTRINGS", "import os")
    out = scripts.run_command("python -c \"import os; print('x')\"", shell="bash", cwd=".", timeout_seconds=5)
    assert "blocked by policy" in out
