"""Task-scoped JWT for /files/import."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from kragen.services import task_token as tt


def _stub_settings(
    *,
    task_enabled: bool = True,
    jwt_secret: str = "x" * 32,
    algorithm: str = "HS256",
) -> object:
    return SimpleNamespace(
        worker=SimpleNamespace(
            task_token_enabled=task_enabled,
            task_token_ttl_seconds=300,
        ),
        auth=SimpleNamespace(jwt_secret=jwt_secret, jwt_algorithm=algorithm),
    )


def test_mint_and_decode_roundtrip() -> None:
    uid = uuid.uuid4()
    wid = uuid.uuid4()
    tid = uuid.uuid4()
    with patch("kragen.services.task_token.get_settings", return_value=_stub_settings()):
        token = tt.mint_task_token(user_id=uid, workspace_id=wid, task_id=tid)
    assert isinstance(token, str) and len(token) > 20

    with patch("kragen.services.task_token.get_settings", return_value=_stub_settings()):
        p = tt.try_decode_task_token(token)
    assert p is not None
    assert p.user_id == uid
    assert p.workspace_id == wid
    assert p.task_id == tid
    assert p.scope == tt.FILE_TASK_SCOPE
    assert p.can_write_file_tree()
    assert p.can_import()


def test_decode_returns_none_when_disabled() -> None:
    uid = uuid.uuid4()
    with patch("kragen.services.task_token.get_settings", return_value=_stub_settings()):
        token = tt.mint_task_token(
            user_id=uid, workspace_id=uuid.uuid4(), task_id=uuid.uuid4()
        )
    with patch(
        "kragen.services.task_token.get_settings", return_value=_stub_settings(task_enabled=False)
    ):
        assert tt.try_decode_task_token(token) is None
