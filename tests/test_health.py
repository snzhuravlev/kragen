"""Smoke tests for HTTP surface."""

from fastapi.testclient import TestClient

from kragen.api.main import create_app


def test_health() -> None:
    client = TestClient(create_app())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
