"""Synchronous persistence for first-post author history."""

import sqlite3
from collections.abc import Collection
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .storage import Migrations, apply_migrations, connect


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS firstpost_seen_authors (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            first_seen_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )


MIGRATIONS: Migrations = (_create_schema,)


class FirstPostStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(connect(self.database_path)) as connection:
            apply_migrations(connection, MIGRATIONS, label="firstpost storage")

    def load_guild(self, guild_id: int) -> set[int]:
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT user_id FROM firstpost_seen_authors WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchall()
        return {int(row[0]) for row in rows}

    def count(self, guild_id: int) -> int:
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM firstpost_seen_authors WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        return int(row[0]) if row else 0

    def flush(self, guild_id: int, author_ids: Collection[int]) -> None:
        first_seen_at = int(datetime.now(timezone.utc).timestamp())
        rows = [
            (
                str(guild_id),
                str(user_id),
                first_seen_at,
            )
            for user_id in author_ids
        ]
        if not rows:
            return
        with closing(connect(self.database_path)) as connection, connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO firstpost_seen_authors
                (guild_id, user_id, first_seen_at)
                VALUES (?, ?, ?)
                """,
                rows,
            )
