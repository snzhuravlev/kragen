"""Task stream backends: pluggable transport for task SSE output.

The in-memory backend is the default and matches the original MVP behaviour:
one asyncio.Queue per task id within a single process. A future Redis-backed
implementation can replace it without touching callers of ``task_stream``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator, Protocol, runtime_checkable

from kragen.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_QUEUED_CHUNKS = 4096


@runtime_checkable
class TaskStreamBackend(Protocol):
    """Pluggable transport for SSE chunks of a task."""

    def register_task(self, task_id: str) -> None:
        ...

    async def push_chunk(self, task_id: str, text: str) -> None:
        ...

    async def complete_task(self, task_id: str) -> None:
        ...

    def is_complete(self, task_id: str) -> bool:
        ...

    def iter_chunks(self, task_id: str) -> AsyncIterator[str]:
        ...


class InMemoryTaskStreamBackend:
    """Single-process backend using asyncio queues.

    Matches the MVP semantics: stream is scoped to one API process, overflow
    drops older chunks, and the buffer is disposed shortly after the last
    client disconnects or the task completes with no listeners.
    """

    def __init__(self, *, max_queued_chunks: int = DEFAULT_MAX_QUEUED_CHUNKS) -> None:
        self._max_queued_chunks = max_queued_chunks
        self._buffers: dict[str, asyncio.Queue[str | None]] = {}
        self._done: set[str] = set()
        self._listeners: dict[str, int] = defaultdict(int)

    def register_task(self, task_id: str) -> None:
        if task_id not in self._buffers:
            self._buffers[task_id] = asyncio.Queue()

    async def push_chunk(self, task_id: str, text: str) -> None:
        self.register_task(task_id)
        q = self._buffers[task_id]
        while q.qsize() >= self._max_queued_chunks:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                logger.warning(
                    "task_stream_chunk_dropped",
                    task_id=task_id,
                    max_queued=self._max_queued_chunks,
                )
        await q.put(text)

    def _schedule_buffer_disposal(self, task_id: str) -> None:
        def _try_pop() -> None:
            if self._listeners.get(task_id, 0) > 0:
                return
            self._buffers.pop(task_id, None)
            self._done.discard(task_id)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _try_pop()
            return
        loop.call_later(2.0, _try_pop)

    async def complete_task(self, task_id: str) -> None:
        self.register_task(task_id)
        await self._buffers[task_id].put(None)
        self._done.add(task_id)
        self._schedule_buffer_disposal(task_id)

    def is_complete(self, task_id: str) -> bool:
        return task_id in self._done

    async def iter_chunks(self, task_id: str) -> AsyncIterator[str]:
        if task_id in self._done and task_id not in self._buffers:
            return
        self.register_task(task_id)
        self._listeners[task_id] += 1
        try:
            while True:
                item = await self._buffers[task_id].get()
                if item is None:
                    break
                yield item
        finally:
            self._listeners[task_id] -= 1
            if self._listeners[task_id] <= 0:
                self._listeners.pop(task_id, None)
            self._buffers.pop(task_id, None)
            self._done.discard(task_id)
