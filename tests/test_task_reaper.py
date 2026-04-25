"""Unit tests for stale task reaper config math."""

from __future__ import annotations

from kragen.services.task_reaper import compute_stale_after_seconds


def test_compute_stale_after_uses_runtime_budget_plus_buffer() -> None:
    value = compute_stale_after_seconds(
        timeout_seconds=180,
        retries=1,
        minimum_stale_after_seconds=300,
    )
    assert value == 480


def test_compute_stale_after_respects_minimum_floor() -> None:
    value = compute_stale_after_seconds(
        timeout_seconds=30,
        retries=0,
        minimum_stale_after_seconds=900,
    )
    assert value == 900
