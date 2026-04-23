"""FastAPI dependencies: DB, auth context, correlation IDs."""

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from kragen.config import get_settings
from kragen.db.session import get_session
from kragen.logging_config import get_logger

logger = get_logger(__name__)
_settings = get_settings()


DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_correlation_id(
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> str:
    """Resolve or generate a correlation ID for tracing."""
    existing = getattr(request.state, "correlation_id", None)
    if existing:
        return str(existing)
    if x_request_id:
        request.state.correlation_id = x_request_id
        return x_request_id
    cid = str(uuid.uuid4())
    request.state.correlation_id = cid
    return cid


CorrelationId = Annotated[str, Depends(get_correlation_id)]


async def require_bearer_user(
    authorization: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """
    MVP auth: optional Bearer token interpreted as raw user UUID for local dev.

    Production should validate JWT and map to user.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return uuid.UUID(token)
    except ValueError as exc:
        logger.warning("invalid_bearer_token", token_prefix=token[:8])
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc


async def get_user_id_for_dev(
    authorization: Annotated[str | None, Header()] = None,
    x_dev_user_id: Annotated[str | None, Header(alias="X-Dev-User-ID")] = None,
) -> uuid.UUID:
    """
    Resolve user id: production Bearer UUID, or dev headers when AUTH_DISABLED=true.
    """
    if _settings.auth.disabled:
        if x_dev_user_id:
            return uuid.UUID(x_dev_user_id)
        if _settings.auth.dev_user_id:
            return uuid.UUID(_settings.auth.dev_user_id)
        raise HTTPException(
            status_code=401,
            detail="AUTH_DISABLED requires X-Dev-User-ID or DEV_USER_ID",
        )
    return await require_bearer_user(authorization)


UserId = Annotated[uuid.UUID, Depends(get_user_id_for_dev)]
