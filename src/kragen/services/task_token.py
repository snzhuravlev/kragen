"""Short-lived JWTs for the Cursor worker to call file APIs as the task user."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt

from kragen.config import get_settings

TASK_TOKEN_TYPE = "kragen_task"
TASK_TOKEN_ISSUER = "kragen"
# Import-only (legacy; still accepted when decoding if minted with older workers).
FILE_IMPORT_SCOPE = "files:import"
# Import + create folders (ensure) + upload — full file-tree write for the task workspace.
FILE_TASK_SCOPE = "files:task"


@dataclass(frozen=True, slots=True)
class TaskTokenPayload:
    """Verified claims from a task-scoped JWT."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    scope: str

    def can_import(self) -> bool:
        return self.scope in (FILE_IMPORT_SCOPE, FILE_TASK_SCOPE)

    def can_write_file_tree(self) -> bool:
        return self.scope == FILE_TASK_SCOPE


def mint_task_token(
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    ttl_seconds: int | None = None,
) -> str:
    """Sign a short-lived token for the worker subprocess and Kragen files MCP (files:task)."""
    s = get_settings()
    if not s.worker.task_token_enabled:
        raise RuntimeError("Task tokens are disabled in configuration")
    ttl = ttl_seconds if ttl_seconds is not None else s.worker.task_token_ttl_seconds
    ttl = max(60, min(int(ttl), 86400))
    now = int(time.time())
    payload: dict[str, str | int] = {
        "iss": TASK_TOKEN_ISSUER,
        "sub": str(user_id),
        "typ": TASK_TOKEN_TYPE,
        "scope": FILE_TASK_SCOPE,
        "workspace_id": str(workspace_id),
        "task_id": str(task_id),
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(
        payload,
        s.auth.jwt_secret,
        algorithm=s.auth.jwt_algorithm,
    )


def try_decode_task_token(token: str) -> TaskTokenPayload | None:
    """
    If token is a valid task JWT, return its payload.
    Scopes: ``files:import`` (import only) or ``files:task`` (import + folder ensure + upload).
    """
    s = get_settings()
    if not s.worker.task_token_enabled:
        return None
    try:
        claims = jwt.decode(
            token,
            s.auth.jwt_secret,
            algorithms=[s.auth.jwt_algorithm],
            issuer=TASK_TOKEN_ISSUER,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        return None

    if claims.get("typ") != TASK_TOKEN_TYPE:
        return None
    sc = str(claims.get("scope") or "")
    if sc not in (FILE_IMPORT_SCOPE, FILE_TASK_SCOPE):
        return None
    try:
        user_id = uuid.UUID(str(claims.get("sub")))
        workspace_id = uuid.UUID(str(claims.get("workspace_id")))
        task_id = uuid.UUID(str(claims.get("task_id")))
    except (TypeError, ValueError):
        return None
    return TaskTokenPayload(user_id=user_id, workspace_id=workspace_id, task_id=task_id, scope=sc)
