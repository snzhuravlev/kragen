"""Tests for kragen-web-search MCP helpers."""

from __future__ import annotations

import json

import httpx

from kragen.mcp import kragen_web_search_mcp as web_mcp


class _DummyResponse:
    def __init__(self, *, payload=None, text: str = "", status_code: int = 200) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("GET", "https://x"), response=httpx.Response(self.status_code))

    def json(self):
        return self._payload


class _DummyClient:
    def __init__(self, responses: list[_DummyResponse]) -> None:
        self._responses = responses
        self._idx = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        response = self._responses[self._idx]
        self._idx += 1
        return response


def test_web_fetch_blocks_private_urls() -> None:
    out = web_mcp.web_fetch("http://localhost:8000")
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "blocked by policy" in payload["warnings"][0]


def test_web_search_parses_related_topics(monkeypatch) -> None:
    payload = {
        "Heading": "Duck",
        "AbstractURL": "https://example.org/a",
        "AbstractText": "Primary entry",
        "RelatedTopics": [
            {"Text": "Topic One - desc", "FirstURL": "https://example.org/one"},
            {"Topics": [{"Text": "Topic Two - desc", "FirstURL": "https://example.org/two"}]},
        ],
    }
    monkeypatch.setattr(web_mcp.httpx, "Client", lambda **kwargs: _DummyClient([_DummyResponse(payload=payload)]))
    out = web_mcp.web_search("duck")
    result = json.loads(out)
    assert result["ok"] is True
    assert len(result["results"]) >= 2


def test_web_fetch_truncates_content(monkeypatch) -> None:
    html = "<html><body>" + ("hello " * 2000) + "</body></html>"
    monkeypatch.setattr(
        web_mcp.httpx,
        "Client",
        lambda **kwargs: _DummyClient([_DummyResponse(text=html)]),
    )
    out = web_mcp.web_fetch("https://example.org/page", max_chars=1000)
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["truncated"] is True
    assert len(payload["content"]) <= 1000
