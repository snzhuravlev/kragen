"""Unit tests for the pluggable task stream backend."""

from __future__ import annotations

import asyncio

import pytest

from kragen.services import task_stream
from kragen.services.task_stream_backends import (
    InMemoryTaskStreamBackend,
    TaskStreamBackend,
)


async def _drain(task_id: str, expected: int) -> list[str]:
    chunks: list[str] = []
    async for chunk in task_stream.iter_chunks(task_id):
        chunks.append(chunk)
        if len(chunks) >= expected:
            break
    return chunks


def test_default_backend_is_in_memory() -> None:
    backend = task_stream.get_backend()
    assert isinstance(backend, InMemoryTaskStreamBackend)


def test_set_backend_replaces_instance() -> None:
    original = task_stream.get_backend()
    try:
        replacement = InMemoryTaskStreamBackend(max_queued_chunks=8)
        task_stream.set_backend(replacement)
        assert task_stream.get_backend() is replacement
    finally:
        task_stream.set_backend(original)


def test_backend_protocol_contract() -> None:
    backend: TaskStreamBackend = InMemoryTaskStreamBackend()
    assert isinstance(backend, TaskStreamBackend)


@pytest.mark.asyncio
async def test_push_and_iter_via_facade() -> None:
    original = task_stream.get_backend()
    task_stream.set_backend(InMemoryTaskStreamBackend(max_queued_chunks=4))
    try:
        task_id = "test-task-1"
        task_stream.register_task(task_id)

        async def producer() -> None:
            await task_stream.push_chunk(task_id, "a")
            await task_stream.push_chunk(task_id, "b")
            await task_stream.complete_task(task_id)

        produced = asyncio.create_task(producer())
        got: list[str] = []
        async for chunk in task_stream.iter_chunks(task_id):
            got.append(chunk)
        await produced

        assert got == ["a", "b"]
        assert task_stream.is_complete(task_id) is False  # disposed after stream end
    finally:
        task_stream.set_backend(original)


@pytest.mark.asyncio
async def test_overflow_drops_oldest() -> None:
    original = task_stream.get_backend()
    task_stream.set_backend(InMemoryTaskStreamBackend(max_queued_chunks=2))
    try:
        task_id = "test-task-overflow"
        task_stream.register_task(task_id)

        for i in range(5):
            await task_stream.push_chunk(task_id, f"chunk-{i}")
        await task_stream.complete_task(task_id)

        chunks = [c async for c in task_stream.iter_chunks(task_id)]
        assert chunks[-1] == "chunk-4"
        assert len(chunks) <= 2
    finally:
        task_stream.set_backend(original)
