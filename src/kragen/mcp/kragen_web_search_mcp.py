"""Stdio MCP server: web search and page fetch for agent tasks."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kragen-web-search")

_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_RESULTS = 5
_DEFAULT_MAX_CONTENT_CHARS = 12_000
_DEFAULT_SAFE_SEARCH = "moderate"

_DUCKDUCKGO_API = "https://api.duckduckgo.com/"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _timeout_seconds() -> float:
    return max(2.0, _float_env("KRAGEN_WEB_SEARCH_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))


def _max_results_default() -> int:
    return max(1, min(20, _int_env("KRAGEN_WEB_SEARCH_MAX_RESULTS", _DEFAULT_MAX_RESULTS)))


def _max_content_chars() -> int:
    return max(500, _int_env("KRAGEN_WEB_SEARCH_MAX_CONTENT_CHARS", _DEFAULT_MAX_CONTENT_CHARS))


def _safe_search_default() -> str:
    raw = os.environ.get("KRAGEN_WEB_SEARCH_SAFE_SEARCH", "").strip().lower()
    if raw in {"off", "moderate", "strict"}:
        return raw
    return _DEFAULT_SAFE_SEARCH


def _allowed_domains() -> list[str]:
    return _csv_env("KRAGEN_WEB_SEARCH_ALLOWED_DOMAINS")


def _allow_http() -> bool:
    raw = (os.environ.get("KRAGEN_WEB_SEARCH_ALLOW_HTTP") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_private_host(host: str) -> bool:
    value = host.strip().strip("[]").lower()
    if value in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(value)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )
    except ValueError:
        return value.endswith(".local")


def _validate_url(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"https", "http"}:
        return "URL scheme must be http or https"
    if parsed.scheme == "http" and not _allow_http():
        return "http URLs are disabled by policy (use https)"
    if not parsed.netloc:
        return "URL must include host"
    host = parsed.hostname or ""
    if not host:
        return "URL host is empty"
    if _is_private_host(host):
        return "private/local network URLs are blocked"
    allowed = _allowed_domains()
    if allowed and not any(host.lower().endswith(suffix) for suffix in allowed):
        return f"host is not in allowed domains: {', '.join(allowed)}"
    return None


def _strip_html(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", text)
    normalized = re.sub(r"\s+", " ", without_tags).strip()
    return normalized


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _extract_related(items: list[object], out: list[dict[str, str]]) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if "Topics" in item and isinstance(item["Topics"], list):
            _extract_related(item["Topics"], out)
            continue
        text = str(item.get("Text") or "").strip()
        first_url = str(item.get("FirstURL") or "").strip()
        if not text or not first_url:
            continue
        out.append(
            {
                "title": text.split(" - ", 1)[0].strip(),
                "url": first_url,
                "snippet": text,
                "source": "duckduckgo",
            }
        )


def _json_result(
    *,
    ok: bool,
    provider: str,
    query: str | None = None,
    url: str | None = None,
    results: list[dict[str, str]] | None = None,
    content: str | None = None,
    warnings: list[str] | None = None,
    duration_ms: int = 0,
    truncated: bool = False,
) -> str:
    payload: dict[str, object] = {
        "ok": ok,
        "provider": provider,
        "duration_ms": duration_ms,
        "truncated": truncated,
        "warnings": warnings or [],
    }
    if query is not None:
        payload["query"] = query
    if url is not None:
        payload["url"] = url
    if results is not None:
        payload["results"] = results
    if content is not None:
        payload["content"] = content
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def web_search(
    query: str,
    max_results: int | None = None,
    lang: str = "ru-ru",
    safe_search: str | None = None,
) -> str:
    """Search the web using a privacy-friendly provider and return normalized results."""
    started = time.monotonic()
    query_text = query.strip()
    if not query_text:
        return "error: query must not be empty"
    limit = max_results if max_results is not None else _max_results_default()
    limit = max(1, min(20, int(limit)))
    safe = (safe_search or _safe_search_default()).strip().lower()
    if safe not in {"off", "moderate", "strict"}:
        safe = _safe_search_default()
    params = {
        "q": query_text,
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
        "kl": lang or "ru-ru",
    }
    warnings: list[str] = []
    try:
        with httpx.Client(
            timeout=_timeout_seconds(),
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        ) as client:
            response = client.get(_DUCKDUCKGO_API, params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        return _json_result(
            ok=False,
            provider="duckduckgo",
            query=query_text,
            results=[],
            warnings=[f"search request failed: {type(exc).__name__}: {exc}"],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    results: list[dict[str, str]] = []
    heading = str(data.get("Heading") or "").strip() if isinstance(data, dict) else ""
    abstract_url = str(data.get("AbstractURL") or "").strip() if isinstance(data, dict) else ""
    abstract_text = str(data.get("AbstractText") or "").strip() if isinstance(data, dict) else ""
    if heading and abstract_url:
        results.append(
            {"title": heading, "url": abstract_url, "snippet": abstract_text or heading, "source": "duckduckgo"}
        )
    if isinstance(data, dict) and isinstance(data.get("RelatedTopics"), list):
        _extract_related(data["RelatedTopics"], results)
    dedup: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in results:
        key = item["url"]
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)
        if len(dedup) >= limit:
            break
    if safe == "strict":
        warnings.append("strict safe_search requested; provider-side filtering is best effort")
    return _json_result(
        ok=True,
        provider="duckduckgo",
        query=query_text,
        results=dedup,
        warnings=warnings,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


@mcp.tool()
def web_fetch(url: str, max_chars: int | None = None) -> str:
    """Fetch one web page and return plain text content."""
    started = time.monotonic()
    reason = _validate_url(url)
    if reason is not None:
        return _json_result(
            ok=False,
            provider="fetch",
            url=url,
            warnings=[f"blocked by policy: {reason}"],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    limit = max_chars if max_chars is not None else _max_content_chars()
    limit = max(500, min(100_000, int(limit)))
    try:
        with httpx.Client(
            timeout=_timeout_seconds(),
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept": "text/html, text/plain;q=0.9, */*;q=0.8"},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            raw = response.text
    except Exception as exc:  # noqa: BLE001
        return _json_result(
            ok=False,
            provider="fetch",
            url=url,
            warnings=[f"fetch request failed: {type(exc).__name__}: {exc}"],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    content, truncated = _truncate(_strip_html(raw), limit)
    return _json_result(
        ok=True,
        provider="fetch",
        url=url,
        content=content,
        warnings=[],
        duration_ms=int((time.monotonic() - started) * 1000),
        truncated=truncated,
    )


@mcp.tool()
def web_search_and_fetch(query: str, max_results: int = 3) -> str:
    """Search and fetch top result pages for quick evidence gathering."""
    started = time.monotonic()
    search_payload = json.loads(
        web_search(
            query=query,
            max_results=max_results,
            lang="ru-ru",
            safe_search=_safe_search_default(),
        )
    )
    if not search_payload.get("ok"):
        return json.dumps(search_payload, ensure_ascii=False, indent=2)
    results = search_payload.get("results", [])
    if not isinstance(results, list):
        results = []
    enriched: list[dict[str, object]] = []
    warnings: list[str] = []
    for item in results[: max(1, min(10, max_results))]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        fetched = json.loads(web_fetch(url=url, max_chars=min(3000, _max_content_chars())))
        entry: dict[str, object] = dict(item)
        entry["fetch_ok"] = bool(fetched.get("ok"))
        entry["content_preview"] = str(fetched.get("content") or "")
        fetched_warnings = fetched.get("warnings")
        if isinstance(fetched_warnings, list):
            warnings.extend(str(x) for x in fetched_warnings)
        enriched.append(entry)
    return _json_result(
        ok=True,
        provider="duckduckgo+fetch",
        query=query.strip(),
        results=[{k: str(v) for k, v in row.items()} for row in enriched],
        warnings=warnings,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
