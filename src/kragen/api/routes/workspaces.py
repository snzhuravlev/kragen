"""Workspace endpoints used by Web/CLI adapters."""

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import or_, select
from sqlalchemy.sql import column, table

from kragen.api.deps import DbSession, UserId
from kragen.api.schemas import WorkspaceOut
from kragen.models.core import Workspace

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    db: DbSession,
    user_id: UserId,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[WorkspaceOut]:
    """List workspaces available to the current user."""
    workspace_members = table(
        "workspace_members",
        column("workspace_id"),
        column("user_id"),
    )
    membership_subquery = select(workspace_members.c.workspace_id).where(
        workspace_members.c.user_id == user_id
    )
    stmt = (
        select(Workspace)
        .where(
            or_(
                Workspace.owner_user_id == user_id,
                Workspace.id.in_(membership_subquery),
            )
        )
        .order_by(Workspace.updated_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
) -> WorkspaceOut:
    """Fetch one workspace if it belongs to the current user."""
    workspace_members = table(
        "workspace_members",
        column("workspace_id"),
        column("user_id"),
    )
    membership_subquery = select(workspace_members.c.workspace_id).where(
        workspace_members.c.user_id == user_id
    )
    stmt = select(Workspace).where(
        Workspace.id == workspace_id,
        or_(
            Workspace.owner_user_id == user_id,
            Workspace.id.in_(membership_subquery),
        ),
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Workspace not found")
    return WorkspaceOut.model_validate(row)
