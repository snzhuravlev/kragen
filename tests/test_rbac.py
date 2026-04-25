"""Unit tests for RBAC helpers and admin YAML masking."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from kragen.api.deps import _decode_jwt_user_id, is_admin_user
from kragen.api.routes.admin import _mask_dsn_password, _mask_sensitive_yaml
from kragen.config import KragenSettings, get_settings


class _Stub:
    """Minimal stub exposing ``auth.admin_user_ids`` for patching."""

    def __init__(self, admin_ids: list[str]) -> None:
        self.auth = type("A", (), {"admin_user_ids": admin_ids})()


def test_is_admin_user_matches_list() -> None:
    admin_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    other_id = uuid.UUID("22222222-2222-2222-2222-222222222222")

    with patch("kragen.api.deps.get_settings", return_value=_Stub([str(admin_id)])):
        assert is_admin_user(admin_id) is True
        assert is_admin_user(other_id) is False


def test_is_admin_user_empty_list_is_noone() -> None:
    admin_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    with patch("kragen.api.deps.get_settings", return_value=_Stub([])):
        assert is_admin_user(admin_id) is False


def test_is_admin_user_case_insensitive() -> None:
    admin_id = uuid.UUID("abcdef12-3456-7890-abcd-ef1234567890")
    with patch("kragen.api.deps.get_settings", return_value=_Stub([str(admin_id).upper()])):
        assert is_admin_user(admin_id) is True


def test_mask_dsn_password_replaces_secret() -> None:
    dsn = "postgresql+asyncpg://kragen:supersecret@db.example:5432/kragen"
    masked = _mask_dsn_password(dsn)
    assert "supersecret" not in masked
    assert "***masked***" in masked
    assert masked.startswith("postgresql+asyncpg://kragen:")
    assert "@db.example:5432/kragen" in masked


def test_mask_sensitive_yaml_replaces_known_fields() -> None:
    src = """
app:
  name: kragen
database:
  url: postgresql+asyncpg://kragen:supersecret@host/db
storage:
  access_key: AKIA123
  secret_key: verysecret
auth:
  jwt_secret: signing-key
telegram_channel:
  bot_token: "123:ABC"
  webhook_secret_token: "xyz"
"""
    out = _mask_sensitive_yaml(src)
    data = yaml.safe_load(out)

    assert "supersecret" not in out
    assert "verysecret" not in out
    assert "signing-key" not in out
    assert "123:ABC" not in out
    assert "xyz" not in out.splitlines()[-2]

    assert data["database"]["url"].endswith("@host/db")
    assert data["storage"]["secret_key"] == "***masked***"
    assert data["auth"]["jwt_secret"] == "***masked***"
    assert data["telegram_channel"]["bot_token"] == "***masked***"
    assert data["telegram_channel"]["webhook_secret_token"] == "***masked***"
    assert data["storage"]["access_key"] == "***masked***"


def test_config_has_admin_user_ids_field() -> None:
    settings = get_settings()
    assert isinstance(settings.auth.admin_user_ids, list)


@pytest.mark.parametrize(
    "dsn",
    [
        "plain-string-without-creds",
        "",
    ],
)
def test_mask_dsn_password_noop_for_non_dsn(dsn: str) -> None:
    assert _mask_dsn_password(dsn) == "***masked***"


def test_prod_config_rejects_dev_auth_and_wildcard_bind() -> None:
    with pytest.raises(ValidationError, match="Invalid production configuration"):
        KragenSettings(
            app={"environment": "prod"},
            api={"host": "0.0.0.0"},
            database={"url": "postgresql+asyncpg://kragen:secret@127.0.0.1:5432/kragen"},
            auth={
                "disabled": True,
                "dev_user_id": "11111111-1111-1111-1111-111111111111",
                "jwt_secret": "change-me-in-production",
            },
        )


def test_prod_config_allows_localhost_bind_with_real_secret() -> None:
    settings = KragenSettings(
        app={"environment": "prod"},
        api={"host": "127.0.0.1"},
        database={"url": "postgresql+asyncpg://kragen:secret@127.0.0.1:5432/kragen"},
        auth={
            "disabled": False,
            "jwt_secret": "not-the-default-secret",
            "dev_user_id": None,
            "raw_uuid_bearer_enabled": False,
        },
    )

    assert settings.api.host == "127.0.0.1"


def test_decode_jwt_user_id_uses_subject_uuid() -> None:
    jwt = pytest.importorskip("jwt")
    user_id = uuid.uuid4()
    secret = "a-production-length-test-secret-value"
    token = jwt.encode({"sub": str(user_id)}, secret, algorithm="HS256")
    settings = KragenSettings(
        database={"url": "postgresql+asyncpg://kragen:secret@127.0.0.1:5432/kragen"},
        auth={"jwt_secret": secret},
    )

    with patch("kragen.api.deps.get_settings", return_value=settings):
        assert _decode_jwt_user_id(token) == user_id
