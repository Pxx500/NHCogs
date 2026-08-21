from __future__ import annotations

import asyncio
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .sqlite_files import is_transient_sqlite_sidecar

_BACKUP_RESERVE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class DatabaseInspection:
    path: str
    size_bytes: int
    integrity_result: str
    table_rows: dict[str, int]
    error: str | None = None


@dataclass(frozen=True)
class PersistedDataReport:
    data_directories: dict[str, str]
    databases: tuple[DatabaseInspection, ...]
    file_count: int
    total_bytes: int
    required_backup_bytes: int
    free_bytes: int
    blocking_issues: tuple[str, ...]

    @property
    def database_count(self) -> int:
        return len(self.databases)


async def inspect_persisted_data(
    data_directories: dict[str, Path],
    *,
    backup_root: Path,
) -> PersistedDataReport:
    return await asyncio.to_thread(
        _inspect_persisted_data_sync,
        data_directories,
        backup_root,
    )


def _inspect_persisted_data_sync(
    data_directories: dict[str, Path],
    backup_root: Path,
) -> PersistedDataReport:
    blocking_issues: list[str] = []
    files: list[Path] = []
    resolved_directories: dict[str, str] = {}
    for name, raw_path in data_directories.items():
        path = raw_path.resolve()
        resolved_directories[name] = str(path)
        if not path.exists():
            blocking_issues.append(f"{name} data directory is missing: {path}")
            continue
        if not path.is_dir():
            blocking_issues.append(f"{name} data path is not a directory: {path}")
            continue
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            if is_transient_sqlite_sidecar(candidate):
                continue
            files.append(candidate)
            try:
                with candidate.open("rb") as file:
                    file.read(0)
            except OSError as error:
                blocking_issues.append(f"Unreadable persisted file {candidate}: {error}")

    databases: list[DatabaseInspection] = []
    for path in sorted(
        (file for file in files if file.suffix.casefold() == ".sqlite"),
        key=str,
    ):
        inspection = _inspect_database(path)
        databases.append(inspection)
        if inspection.error is not None or inspection.integrity_result != "ok":
            detail = inspection.error or inspection.integrity_result
            blocking_issues.append(f"SQLite integrity failed for {path}: {detail}")

    total_bytes = sum(_file_size(path, blocking_issues) for path in files)
    database_bytes = sum(database.size_bytes for database in databases)
    backup_payload_bytes = total_bytes + database_bytes
    required_backup_bytes = (
        backup_payload_bytes
        + max(_BACKUP_RESERVE_BYTES, backup_payload_bytes // 10)
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    free_bytes = int(shutil.disk_usage(backup_root).free)
    if free_bytes < required_backup_bytes:
        blocking_issues.append(
            "Insufficient backup space: "
            f"need {required_backup_bytes} bytes, have {free_bytes} bytes"
        )

    return PersistedDataReport(
        data_directories=resolved_directories,
        databases=tuple(databases),
        file_count=len(files),
        total_bytes=total_bytes,
        required_backup_bytes=required_backup_bytes,
        free_bytes=free_bytes,
        blocking_issues=tuple(blocking_issues),
    )


def _inspect_database(path: Path) -> DatabaseInspection:
    size_bytes = path.stat().st_size
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity_result = ", ".join(str(row[0]) for row in integrity_rows)
            tables = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_schema
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            )
            table_rows = {
                table: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
                    ).fetchone()[0]
                )
                for table in tables
            }
        return DatabaseInspection(
            path=str(path),
            size_bytes=size_bytes,
            integrity_result=integrity_result,
            table_rows=table_rows,
        )
    except (OSError, sqlite3.Error) as error:
        return DatabaseInspection(
            path=str(path),
            size_bytes=size_bytes,
            integrity_result="error",
            table_rows={},
            error=str(error),
        )


def _file_size(path: Path, blocking_issues: list[str]) -> int:
    try:
        return path.stat().st_size
    except OSError as error:
        blocking_issues.append(f"Could not stat persisted file {path}: {error}")
        return 0
