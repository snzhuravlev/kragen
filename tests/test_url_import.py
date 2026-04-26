"""URL fetch policy for server-side file import."""

from __future__ import annotations

import pytest

from kragen.config import FileImportSettings
from kragen.services.url_import import UrlImportError, fetch_url_bytes


@pytest.mark.asyncio
async def test_rejects_non_http_scheme() -> None:
    s = FileImportSettings()
    with pytest.raises(UrlImportError, match="Only http"):
        await fetch_url_bytes("ftp://a/b", settings=s)


@pytest.mark.asyncio
async def test_rejects_host_not_in_allowlist() -> None:
    s = FileImportSettings(allowed_host_suffixes=["expected.example.com"])
    with pytest.raises(UrlImportError, match="not allowed"):
        await fetch_url_bytes("https://other.com/file", settings=s)
