"""The config_revisions table: what changed in config.yaml, who changed it, and undo."""

from dataclasses import dataclass
from datetime import datetime

import discord
import psycopg

from utils import db

UNDOABLE = ("edit", "undo")

_COLUMNS = "id, changed_at, action, path, user_name, before_text, after_text, undone_by"


@dataclass(frozen=True)
class Revision:
    id: int
    changed_at: datetime
    action: str
    path: str | None
    user_name: str | None
    before_text: str | None
    after_text: str
    undone_by: int | None

    @property
    def undoable(self) -> bool:
        return self.action in UNDOABLE and self.undone_by is None


async def record(
    action: str,
    after_text: str,
    before_text: str | None = None,
    path: str | None = None,
    member: discord.abc.User | None = None,
) -> int:
    row = await db.fetch_one(
        """
        INSERT INTO config_revisions
            (action, path, user_id, user_name, before_text, after_text)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (
            action,
            path,
            member.id if member else None,
            member.name if member else None,
            before_text,
            after_text,
        ),
    )
    assert row is not None
    return row[0]


async def get(revision_id: int) -> Revision | None:
    row = await db.fetch_one(
        f"SELECT {_COLUMNS} FROM config_revisions WHERE id = %s;", (revision_id,)
    )
    return Revision(*row) if row else None


async def recent(limit: int = 10, path: str | None = None) -> list[Revision]:
    rows = await db.fetch_all(
        f"""
        SELECT {_COLUMNS} FROM config_revisions
        WHERE %s::text IS NULL OR path = %s
        ORDER BY id DESC LIMIT %s;
        """,
        (path, path, limit),
    )
    return [Revision(*row) for row in rows]


async def latest_text() -> str | None:
    row = await db.fetch_one(
        "SELECT after_text FROM config_revisions ORDER BY id DESC LIMIT 1;"
    )
    return row[0] if row else None


async def mark_undone(revision_id: int, undone_by: int) -> None:
    await db.perform_one(
        "UPDATE config_revisions SET undone_by = %s WHERE id = %s;",
        (undone_by, revision_id),
    )


def latest_text_before_startup() -> str | None:
    """latest_text without the pool, which doesn't exist yet when config first loads."""
    with (
        psycopg.connect(db.get_db_conninfo(), connect_timeout=5) as conn,
        conn.cursor() as cur,
    ):
        try:
            cur.execute(
                "SELECT after_text FROM config_revisions ORDER BY id DESC LIMIT 1;"
            )
        except psycopg.errors.UndefinedTable:
            return None
        row = cur.fetchone()
    return row[0] if row else None
