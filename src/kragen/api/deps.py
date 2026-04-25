"""FastAPI dependencies: DB, auth context, correlation IDs, RBAC."""

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
import jwt
from jwt import PyJWKClient
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import column, table

from kragen.config import get_settings
from kragen.db.session import get_session
from kragen.logging_config import get_logger
from kragen.models.core import Workspace

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
    Resolve Bearer credentials to a user UUID.

    Production validates JWT/OIDC tokens. Raw UUID bearer tokens are retained as
    an explicit development/legacy fallback only.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    settings = get_settings()
    token_errors: list[str] = []

    if "." in token or not settings.auth.raw_uuid_bearer_enabled:
        try:
            return _decode_jwt_user_id(token)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            token_errors.append(f"{type(exc).__name__}: {exc}")

    if not settings.auth.raw_uuid_bearer_enabled:
        logger.warning("invalid_jwt_token", token_prefix=token[:8], errors=token_errors)
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    try:
        return uuid.UUID(token)
    except ValueError as exc:
        logger.warning("invalid_bearer_token", token_prefix=token[:8])
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc


def _decode_jwt_user_id(token: str) -> uuid.UUID:
    """Validate JWT/OIDC bearer token and return the subject UUID."""
    settings = get_settings().auth
    algorithm = settings.jwt_algorithm
    decode_kwargs: dict[str, object] = {
        "algorithms": [algorithm],
        "options": {"verify_aud": settings.jwt_audience is not None},
    }
    if settings.jwt_issuer:
        decode_kwargs["issuer"] = settings.jwt_issuer
    if settings.jwt_audience:
        decode_kwargs["audience"] = settings.jwt_audience

    try:
        if settings.oidc_jwks_url:
            signing_key = PyJWKClient(settings.oidc_jwks_url).get_signing_key_from_jwt(token)
            claims = jwt.decode(token, signing_key.key, **decode_kwargs)
        else:
            claims = jwt.decode(token, settings.jwt_secret, **decode_kwargs)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc

    subject = claims.get("sub") or claims.get("user_id")
    if not subject:
        raise HTTPException(status_code=401, detail="JWT subject is required")
    try:
        return uuid.UUID(str(subject))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="JWT subject must be a UUID") from exc


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


def is_admin_user(user_id: uuid.UUID) -> bool:
    """Return True when the user is listed in auth.admin_user_ids.

    Reads settings fresh to honor admin list updates after cache invalidation.
    """
    admin_ids = {str(x).strip().lower() for x in get_settings().auth.admin_user_ids if x}
    return str(user_id).lower() in admin_ids


async def require_admin_user(user_id: UserId) -> uuid.UUID:
    """Reject the request when the current user is not an admin."""
    if not is_admin_user(user_id):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user_id


AdminUserId = Annotated[uuid.UUID, Depends(require_admin_user)]


async def ensure_workspace_access(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Ensure the user can read data scoped to the given workspace.

    Admins pass unconditionally. Other users must either own the workspace or
    appear in ``workspace_members``.
    """
    if is_admin_user(user_id):
        return

    workspace_members = table(
        "workspace_members",
        column("workspace_id"),
        column("user_id"),
    )
    membership_subquery = select(workspace_members.c.workspace_id).where(
        workspace_members.c.user_id == user_id,
        workspace_members.c.workspace_id == workspace_id,
    )
    stmt = select(Workspace.id).where(
        Workspace.id == workspace_id,
        or_(
            Workspace.owner_user_id == user_id,
            Workspace.id.in_(membership_subquery),
        ),
    )
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="No access to this workspace")
