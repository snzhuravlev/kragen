"""Seed minimal user/workspace for local development."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from kragen.db.session import async_session_factory
from kragen.models.core import User, Workspace


DEV_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def main() -> None:
    async with async_session_factory() as db:
        existing = await db.execute(select(User).where(User.id == DEV_USER_ID))
        if existing.scalar_one_or_none():
            print("Seed already applied.")
            return

        user = User(
            id=DEV_USER_ID,
            email="dev@example.com",
            display_name="Dev User",
        )
        ws = Workspace(
            name="Default",
            slug="default",
            owner_user_id=DEV_USER_ID,
        )
        db.add(user)
        db.add(ws)
        await db.commit()
        print(f"Seeded user {DEV_USER_ID} and workspace {ws.id}")


if __name__ == "__main__":
    asyncio.run(main())
