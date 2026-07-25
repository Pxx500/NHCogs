"""Shared synchronous SQLite plumbing for Honeypot stores."""

from collections.abc import Callable, Sequence
from pathlib import Path
import sqlite3
from typing import TypeAlias


ConnectionFactory: TypeAlias = Callable[..., sqlite3.Connection]
Migration: TypeAlias = Callable[[sqlite3.Connection], None]
Migrations: TypeAlias = Sequence[Migration]


def connect(
    path: str | Path,
    *,
    connection_factory: ConnectionFactory = sqlite3.connect,
) -> sqlite3.Connection:
    connection = connection_factory(str(path), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Migrations,
    *,
    label: str,
) -> None:
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    supported_version = len(migrations)
    if current_version < 0:
        raise ValueError(f"{label} schema version {current_version} is invalid")
    if current_version > supported_version:
        raise ValueError(
            f"{label} schema version {current_version} is newer than supported "
            f"version {supported_version}"
        )

    for index in range(current_version, supported_version):
        try:
            connection.execute("BEGIN")
            migrations[index](connection)
            connection.execute(f"PRAGMA user_version = {index + 1}")
            connection.commit()
        except Exception as error:
            connection.rollback()
            raise RuntimeError(f"{label} migration {index} failed") from error
