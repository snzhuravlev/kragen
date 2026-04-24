"""Task stream facade: public API + pluggable backend (in-memory by default).

Callers use module-level functions (`register_task`, `push_chunk`, etc.) and
stay decoupled from the backend implementation. The backend is chosen by
`kragen.services.task_stream.get_backend()` and can be swapped at process
startup via `set_backend(...)` — typically driven by configuration.
"""

from __future__ import annotations

from typing import AsyncIterator

from kragen.services.task_stream_backends import (
    InMemoryTaskStreamBackend,
    TaskStreamBackend,
)

_backend: TaskStreamBackend = InMemoryTaskStreamBackend()


def get_backend() -> TaskStreamBackend:
    """Return the active task stream backend."""
    return _backend


def set_backend(backend: TaskStreamBackend) -> None:
    """Override the active backend (for tests or alternative transports)."""
    global _backend
    _backend = backend


def register_task(task_id: str) -> None:
    """Ensure resources exist for a task stream."""
    _backend.register_task(task_id)


async def push_chunk(task_id: str, text: str) -> None:
    """Append a streamed text chunk for subscribers."""
    await _backend.push_chunk(task_id, text)


async def complete_task(task_id: str) -> None:
    """Signal end of stream for a task."""
    await _backend.complete_task(task_id)


def is_complete(task_id: str) -> bool:
    """Return True if task stream finished."""
    return _backend.is_complete(task_id)


def iter_chunks(task_id: str) -> AsyncIterator[str]:
    """Async iterator over output chunks for SSE consumers."""
    return _backend.iter_chunks(task_id)
