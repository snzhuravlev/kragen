"""In-process task output buffer for SSE streaming (MVP; replace with Redis for multi-worker)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator

from kragen.logging_config import get_logger

logger = get_logger(__name__)

# Cap queued chunks per task so unbounded output cannot exhaust RAM.
_MAX_QUEUED_CHUNKS = 4096

_buffers: dict[str, asyncio.Queue[str | None]] = {}
_done: set[str] = set()
# Active SSE iterators per task (best-effort; multiple tabs share one queue — MVP limitation).
_listeners: dict[str, int] = defaultdict(int)


def register_task(task_id: str) -> None:
    """Ensure a queue exists for a task."""
    if task_id not in _buffers:
        _buffers[task_id] = asyncio.Queue()


async def push_chunk(task_id: str, text: str) -> None:
    """Append a streamed text chunk for subscribers."""
    register_task(task_id)
    q = _buffers[task_id]
    while q.qsize() >= _MAX_QUEUED_CHUNKS:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
        else:
            logger.warning(
                "task_stream_chunk_dropped",
                task_id=task_id,
                max_queued=_MAX_QUEUED_CHUNKS,
            )
    await q.put(text)


def _schedule_buffer_disposal(task_id: str) -> None:
    """If no SSE client ever connects, drop the queue after a short grace period."""

    def _try_pop() -> None:
        if _listeners.get(task_id, 0) > 0:
            return
        _buffers.pop(task_id, None)
        _done.discard(task_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _try_pop()
        return
    loop.call_later(2.0, _try_pop)


async def complete_task(task_id: str) -> None:
    """Signal end of stream."""
    register_task(task_id)
    await _buffers[task_id].put(None)
    _done.add(task_id)
    _schedule_buffer_disposal(task_id)


def is_complete(task_id: str) -> bool:
    """Return True if task stream finished."""
    return task_id in _done


async def iter_chunks(task_id: str) -> AsyncIterator[str]:
    """Async iterator for SSE consumers."""
    if task_id in _done and task_id not in _buffers:
        return
    register_task(task_id)
    _listeners[task_id] += 1
    try:
        while True:
            item = await _buffers[task_id].get()
            if item is None:
                break
            yield item
    finally:
        _listeners[task_id] -= 1
        if _listeners[task_id] <= 0:
            _listeners.pop(task_id, None)
        _buffers.pop(task_id, None)
        _done.discard(task_id)
