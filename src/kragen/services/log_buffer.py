"""In-process ring buffer of recent structured log lines (JSON) for the Diagnostics UI."""

from __future__ import annotations

import threading
from collections import deque

_DEFAULT_MAX_LINES = 8000

_lock = threading.Lock()
_lines: deque[str] = deque(maxlen=_DEFAULT_MAX_LINES)


def append_line(line: str) -> None:
    """Append one log line (caller should pass a single JSON line without trailing newline)."""
    if not line:
        return
    with _lock:
        _lines.append(line)


def get_recent_lines(*, limit: int = 500) -> list[str]:
    """Return up to `limit` most recent lines, oldest first."""
    if limit < 1:
        return []
    with _lock:
        n = min(limit, len(_lines))
        if n == 0:
            return []
        start = len(_lines) - n
        return [_lines[i] for i in range(start, len(_lines))]


def clear() -> None:
    """Clear the buffer (e.g. after operator review)."""
    with _lock:
        _lines.clear()


def stats() -> dict[str, int | None]:
    """Return buffer length and capacity."""
    with _lock:
        cap = _lines.maxlen
        return {"count": len(_lines), "maxlen": cap}
