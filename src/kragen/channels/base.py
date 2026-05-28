"""Internal channel gateway contract for out-of-process adapters."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

import httpx

from kragen.channels.telegram.settings import TelegramChannelSettings


@runtime_checkable
class ChannelGateway(Protocol):
    """Minimal API surface a channel adapter uses to talk to Kragen core."""

    async def post_message(
        self,
        *,
        session_id: uuid.UUID,
        text: str,
        metadata: dict[str, Any],
    ) -> uuid.UUID:
        """Append a user message and return the created task id."""
        ...

    async def wait_task(self, *, task_id: uuid.UUID) -> dict[str, Any]:
        """Poll task status until terminal state."""
        ...

    async def stream_task_progress(
        self,
        *,
        task_id: uuid.UUID,
        on_text: Callable[[str], Awaitable[None]],
    ) -> None:
        """Consume SSE chunks and invoke ``on_text`` with aggregated output."""
        ...

    async def last_assistant_message(self, *, session_id: uuid.UUID) -> str:
        """Return the latest assistant message content for a session."""
        ...


class KragenApiGateway:
    """HTTP implementation of :class:`ChannelGateway` for the Kragen REST API."""

    _STREAM_EDIT_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        settings: TelegramChannelSettings,
    ) -> None:
        self._client = client
        self._settings = settings

    async def post_message(
        self,
        *,
        session_id: uuid.UUID,
        text: str,
        metadata: dict[str, Any],
    ) -> uuid.UUID:
        from kragen.channels.telegram.utils import headers

        response = await self._client.post(
            f"{self._settings.kragen_api_base_url}/sessions/{session_id}/messages",
            json={"role": "user", "content": text, "metadata": metadata},
            headers=headers(self._settings),
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        return uuid.UUID(payload["task"]["id"])

    async def wait_task(self, *, task_id: uuid.UUID) -> dict[str, Any]:
        import asyncio

        from kragen.channels.telegram.utils import headers

        deadline = asyncio.get_running_loop().time() + self._settings.task_wait_timeout_seconds
        while True:
            response = await self._client.get(
                f"{self._settings.kragen_api_base_url}/tasks/{task_id}",
                headers=headers(self._settings),
                timeout=20.0,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"Unexpected task payload shape for task {task_id}: {type(data)}")
            status = str(data.get("status", "")).lower()
            if status in {"completed", "failed"}:
                return data
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Task {task_id} did not complete within timeout")
            await asyncio.sleep(self._settings.task_poll_interval_seconds)

    async def stream_task_progress(
        self,
        *,
        task_id: uuid.UUID,
        on_text: Callable[[str], Awaitable[None]],
    ) -> None:
        import asyncio
        import json

        from kragen.channels.telegram.utils import headers

        url = f"{self._settings.kragen_api_base_url}/tasks/{task_id}/stream"
        req_headers = headers(self._settings)
        req_headers["Accept"] = "text/event-stream"
        buffer = ""
        last_emit = 0.0
        async with self._client.stream("GET", url, headers=req_headers, timeout=None) as response:
            response.raise_for_status()
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                try:
                    decoded = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(decoded, str):
                    continue
                buffer += decoded
                now = asyncio.get_running_loop().time()
                if now - last_emit >= self._STREAM_EDIT_INTERVAL_SECONDS:
                    await on_text(buffer)
                    last_emit = now
            if buffer:
                await on_text(buffer)

    async def last_assistant_message(self, *, session_id: uuid.UUID) -> str:
        from kragen.channels.telegram.utils import headers

        response = await self._client.get(
            f"{self._settings.kragen_api_base_url}/sessions/{session_id}/messages",
            headers=headers(self._settings),
            timeout=20.0,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return ""
        for item in reversed(data):
            if isinstance(item, dict) and item.get("role") == "assistant":
                content = item.get("content")
                if isinstance(content, str):
                    return content
        return ""
