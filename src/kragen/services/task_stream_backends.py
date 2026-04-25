"""Task stream backends: pluggable transport for task SSE output."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

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


class RedisTaskStreamBackend:
    """Redis Streams backend shared by API and worker processes."""

    def __init__(
        self,
        *,
        redis_url: str,
        redis_prefix: str = "kragen:task-stream",
        ttl_seconds: int = 3600,
        block_timeout_ms: int = 5000,
    ) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depends on optional runtime install
            raise RuntimeError("Redis task streams require the 'redis' package") from exc

        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._prefix = redis_prefix.rstrip(":")
        self._ttl_seconds = ttl_seconds
        self._block_timeout_ms = block_timeout_ms
        self._local_done: set[str] = set()

    def _stream_key(self, task_id: str) -> str:
        return f"{self._prefix}:{task_id}:chunks"

    def _done_key(self, task_id: str) -> str:
        return f"{self._prefix}:{task_id}:done"

    def register_task(self, task_id: str) -> None:
        # Redis resources are created lazily on first push; keep the protocol synchronous.
        _ = task_id

    async def push_chunk(self, task_id: str, text: str) -> None:
        stream_key = self._stream_key(task_id)
        await self._redis.xadd(stream_key, {"text": text})
        await self._redis.expire(stream_key, self._ttl_seconds)

    async def complete_task(self, task_id: str) -> None:
        stream_key = self._stream_key(task_id)
        await self._redis.xadd(stream_key, {"done": "1"})
        await self._redis.expire(stream_key, self._ttl_seconds)
        await self._redis.set(self._done_key(task_id), "1", ex=self._ttl_seconds)
        self._local_done.add(task_id)

    def is_complete(self, task_id: str) -> bool:
        return task_id in self._local_done

    async def iter_chunks(self, task_id: str) -> AsyncIterator[str]:
        stream_key = self._stream_key(task_id)
        last_id = "0-0"
        while True:
            rows = await self._redis.xread(
                {stream_key: last_id},
                count=32,
                block=self._block_timeout_ms,
            )
            if not rows:
                if await self._redis.exists(self._done_key(task_id)):
                    break
                continue

            for _key, messages in rows:
                for message_id, fields in messages:
                    last_id = message_id
                    if fields.get("done") == "1":
                        self._local_done.add(task_id)
                        return
                    text = fields.get("text")
                    if text is not None:
                        yield text
