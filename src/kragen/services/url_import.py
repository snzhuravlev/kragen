"""Download bytes from a remote URL with host policy and size limits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import httpx

from kragen.config import FileImportSettings


class UrlImportError(Exception):
    """User-facing error for blocked or failed import."""


@dataclass(frozen=True, slots=True)
class FetchedObject:
    """Downloaded object metadata and body."""

    body: bytes
    content_type: str | None
    filename_hint: str | None


def _host_allowed(host: str, allowed_suffixes: list[str]) -> bool:
    h = host.lower().strip()
    if not h:
        return False
    if not allowed_suffixes:
        return True
    for suffix in allowed_suffixes:
        s = suffix.lower().strip().lstrip(".")
        if h == s or h.endswith(f".{s}"):
            return True
    return False


def _filename_from_content_disposition(raw: str | None) -> str | None:
    if not raw:
        return None
    m = re.search(
        r"filename\*?=(?:UTF-8''|)([^;]+)",
        raw,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    name = m.group(1).strip().strip('"')
    if not name:
        return None
    if name.lower().startswith("utf-8''"):
        name = name[7:]
    return unquote(name) or None


def _default_filename_from_url(path: str) -> str:
    p = path.strip() or "/"
    last = PurePosixPath(p).name
    return last if last and last not in ("/", ".") else "download.bin"


def check_fetched_mime(
    content_type: str | None, *, settings: FileImportSettings
) -> None:
    """Raise UrlImportError when Content-Type does not satisfy allowed_mime_prefixes."""
    if not settings.allowed_mime_prefixes:
        return
    if not content_type:
        raise UrlImportError(
            "Response has no Content-Type, but file_import.allowed_mime_prefixes is set"
        )
    ok = any(
        content_type.lower().startswith(p.lower().strip()) for p in settings.allowed_mime_prefixes if p
    )
    if not ok:
        raise UrlImportError(
            f"Content-Type {content_type!r} is not allowed by file_import.allowed_mime_prefixes"
        )


async def fetch_url_bytes(
    url: str,
    *,
    settings: FileImportSettings,
) -> FetchedObject:
    """
    GET url with redirect following, enforce host allowlist and max body size.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        raise UrlImportError(f"Invalid URL: {exc}") from exc
    if parsed.scheme not in ("https", "http"):
        raise UrlImportError("Only http and https URLs are allowed")
    host = parsed.hostname
    if not host:
        raise UrlImportError("URL has no host")
    if not _host_allowed(host, settings.allowed_host_suffixes):
        raise UrlImportError("Remote host is not allowed by server policy")

    timeout = httpx.Timeout(
        connect=min(10.0, float(settings.timeout_seconds)),
        read=float(settings.timeout_seconds),
        write=min(10.0, float(settings.timeout_seconds)),
        pool=10.0,
    )
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
    ) as client:
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                cl = response.headers.get("content-length")
                if cl is not None:
                    try:
                        if int(cl) > settings.max_bytes:
                            raise UrlImportError("Remote file is larger than the configured limit")
                    except ValueError:
                        pass
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip() or None
                cd = response.headers.get("content-disposition")
                name_hint = _filename_from_content_disposition(cd)
                if not name_hint and content_type and "name=" in (cd or ""):
                    name_hint = _filename_from_content_disposition(f"x; {cd}")

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if len(body) > settings.max_bytes:
                        raise UrlImportError("Download exceeded the configured size limit")
        except httpx.HTTPStatusError as exc:
            raise UrlImportError(f"HTTP {exc.response.status_code} when fetching URL") from exc
        except httpx.HTTPError as exc:
            raise UrlImportError(f"Network error: {type(exc).__name__}") from exc

    if not body:
        raise UrlImportError("Empty response body")

    if not name_hint:
        name_hint = _default_filename_from_url(parsed.path)
    check_fetched_mime(content_type, settings=settings)
    return FetchedObject(body=bytes(body), content_type=content_type, filename_hint=name_hint)
