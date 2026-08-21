"""Synchronous persistence for image-scan samples and reporting data."""

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from .image_detector import ImageSample, rebuild_model_state
from .storage import Migrations, apply_migrations, connect

IMAGE_SCAN_PROFILE_COLUMNS = (
    "messages_scanned",
    "messages_with_images",
    "images_considered",
    "images_ignored_over_limit",
    "exact_tp_hits",
    "flagged_tp_hits",
    "download_ms_total",
    "download_ms_count",
    "hash_ms_total",
    "hash_ms_count",
    "compare_ms_total",
    "compare_ms_count",
    "decision_ms_total",
    "decision_ms_count",
)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS imagescan_events (
            event_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            message_jump_url TEXT NOT NULL,
            review_channel_id TEXT,
            review_message_id TEXT,
            created_at INTEGER NOT NULL,
            image_count INTEGER NOT NULL,
            content TEXT,
            decision TEXT NOT NULL DEFAULT 'pending',
            moderator_id TEXT,
            decided_at INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS imagescan_events_message_idx
        ON imagescan_events (guild_id, message_id)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS imagescan_files (
            event_id TEXT NOT NULL,
            file_index INTEGER NOT NULL,
            filename TEXT NOT NULL,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            content_type TEXT,
            sha256 TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            PRIMARY KEY (event_id, file_index)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS imagescan_samples (
            sample_id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            phash TEXT NOT NULL,
            dhash TEXT NOT NULL,
            ahash TEXT NOT NULL,
            source_message_id TEXT,
            source_channel_id TEXT,
            source_jump_url TEXT,
            file_path TEXT,
            file_size_bytes INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            moderator_id TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute("DROP INDEX IF EXISTS imagescan_samples_sha_idx")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS imagescan_samples_sha_idx
        ON imagescan_samples (guild_id, sha256)
        WHERE active = 1
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS imagescan_model_state (
            guild_id TEXT PRIMARY KEY,
            configured_threshold INTEGER NOT NULL DEFAULT 20,
            effective_threshold INTEGER NOT NULL DEFAULT 20,
            max_tp_nearest_score INTEGER,
            min_fp_to_tp_score INTEGER,
            gap INTEGER,
            sample_count_tp INTEGER NOT NULL DEFAULT 0,
            sample_count_fp INTEGER NOT NULL DEFAULT 0,
            stored_size_bytes INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS imagescan_profile (
            guild_id TEXT PRIMARY KEY,
            messages_scanned INTEGER NOT NULL DEFAULT 0,
            messages_with_images INTEGER NOT NULL DEFAULT 0,
            images_considered INTEGER NOT NULL DEFAULT 0,
            images_ignored_over_limit INTEGER NOT NULL DEFAULT 0,
            exact_tp_hits INTEGER NOT NULL DEFAULT 0,
            flagged_tp_hits INTEGER NOT NULL DEFAULT 0,
            download_ms_total INTEGER NOT NULL DEFAULT 0,
            download_ms_count INTEGER NOT NULL DEFAULT 0,
            hash_ms_total INTEGER NOT NULL DEFAULT 0,
            hash_ms_count INTEGER NOT NULL DEFAULT 0,
            compare_ms_total INTEGER NOT NULL DEFAULT 0,
            compare_ms_count INTEGER NOT NULL DEFAULT 0,
            decision_ms_total INTEGER NOT NULL DEFAULT 0,
            decision_ms_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )


MIGRATIONS: Migrations = (_create_schema,)


class ImageScanStore:
    def __init__(
        self,
        database_path: str | Path,
        files_path: str | Path,
        connection_factory: Callable[..., sqlite3.Connection] | None = None,
    ):
        self.database_path = Path(database_path)
        self.files_path = Path(files_path)
        self.connection_factory = connection_factory

    def _connect(self) -> sqlite3.Connection:
        return connect(
            self.database_path,
            connection_factory=self.connection_factory or sqlite3.connect,
        )

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.files_path.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            apply_migrations(connection, MIGRATIONS, label="imagescan storage")

    @staticmethod
    def _sample_from_row(row: sqlite3.Row) -> ImageSample:
        return ImageSample(
            sample_id=str(row["sample_id"]),
            decision=str(row["decision"]),
            sha256=str(row["sha256"]),
            phash=str(row["phash"]),
            dhash=str(row["dhash"]),
            ahash=str(row["ahash"]),
        )

    def load_active(self, guild_id: int) -> list[ImageSample]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sample_id, decision, sha256, phash, dhash, ahash
                FROM imagescan_samples
                WHERE guild_id = ? AND active = 1
                """,
                (str(guild_id),),
            ).fetchall()
        return [self._sample_from_row(row) for row in rows]

    def verify(self, guild_id: int, configured_threshold: int) -> dict[str, Any]:
        samples = self.load_active(guild_id)
        state = rebuild_model_state(samples, configured_threshold)
        state["stored_size_bytes"] = self.stored_size(guild_id)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO imagescan_model_state (
                    guild_id, configured_threshold, effective_threshold,
                    max_tp_nearest_score, min_fp_to_tp_score, gap,
                    sample_count_tp, sample_count_fp, stored_size_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    configured_threshold = excluded.configured_threshold,
                    effective_threshold = excluded.effective_threshold,
                    max_tp_nearest_score = excluded.max_tp_nearest_score,
                    min_fp_to_tp_score = excluded.min_fp_to_tp_score,
                    gap = excluded.gap,
                    sample_count_tp = excluded.sample_count_tp,
                    sample_count_fp = excluded.sample_count_fp,
                    stored_size_bytes = excluded.stored_size_bytes
                """,
                (
                    str(guild_id),
                    int(state["configured_threshold"]),
                    int(state["effective_threshold"]),
                    state.get("max_tp_nearest_score"),
                    state.get("min_fp_to_tp_score"),
                    state.get("gap"),
                    int(state["sample_count_tp"]),
                    int(state["sample_count_fp"]),
                    int(state["stored_size_bytes"]),
                ),
            )
        return state

    def stored_size(self, guild_id: int) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(file_size_bytes), 0)
                FROM imagescan_samples
                WHERE guild_id = ? AND active = 1
                """,
                (str(guild_id),),
            ).fetchone()
        return int(row[0] or 0)

    def profile(self, guild_id: int) -> dict[str, int]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT {', '.join(IMAGE_SCAN_PROFILE_COLUMNS)} "
                "FROM imagescan_profile WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        if row is None:
            return dict.fromkeys(IMAGE_SCAN_PROFILE_COLUMNS, 0)
        return {column: int(row[column] or 0) for column in IMAGE_SCAN_PROFILE_COLUMNS}

    def increment_profile(self, guild_id: int, increments: Mapping[str, int]) -> None:
        if not increments:
            return
        filtered = {
            key: int(value)
            for key, value in increments.items()
            if key in IMAGE_SCAN_PROFILE_COLUMNS and value
        }
        if not filtered:
            return
        columns = ["guild_id", *filtered.keys()]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{key} = {key} + excluded.{key}" for key in filtered)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""
                INSERT INTO imagescan_profile ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(guild_id) DO UPDATE SET {updates}
                """,
                (str(guild_id), *filtered.values()),
            )

    def insert(self, sample: Mapping[str, Any]) -> str:
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                """
                SELECT decision
                FROM imagescan_samples
                WHERE guild_id = ? AND sha256 = ? AND active = 1
                """,
                (sample["guild_id"], sample["sha256"]),
            ).fetchone()
            if existing is not None:
                return (
                    "duplicate"
                    if existing["decision"] == sample["decision"]
                    else "conflict"
                )
            connection.execute(
                """
                INSERT INTO imagescan_samples (
                    sample_id, guild_id, decision, sha256, phash, dhash, ahash,
                    source_message_id, source_channel_id, source_jump_url,
                    file_path, file_size_bytes, created_at, moderator_id, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    sample["sample_id"],
                    sample["guild_id"],
                    sample["decision"],
                    sample["sha256"],
                    sample["phash"],
                    sample["dhash"],
                    sample["ahash"],
                    sample.get("source_message_id"),
                    sample.get("source_channel_id"),
                    sample.get("source_jump_url"),
                    sample.get("file_path"),
                    int(sample.get("file_size_bytes") or 0),
                    int(sample["created_at"]),
                    sample.get("moderator_id"),
                ),
            )
        return "inserted"

    def publish_file_sample(
        self,
        sample: Mapping[str, Any],
        data: bytes,
        path: Path,
    ) -> str:
        # Intentional exception: filesystem publication stays inside the explicit
        # SQLite transaction so the canonical file and sample row commit together.
        temp_path = path.with_name(f".sample-{uuid4().hex}.tmp")
        published = False
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """SELECT decision FROM imagescan_samples
                       WHERE guild_id = ? AND sha256 = ? AND active = 1""",
                    (sample["guild_id"], sample["sha256"]),
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    return (
                        "duplicate"
                        if existing["decision"] == sample["decision"]
                        else "conflict"
                    )
                if path.exists():
                    connection.rollback()
                    return "conflict"
                temp_path.write_bytes(data)
                connection.execute(
                    """INSERT INTO imagescan_samples (
                           sample_id, guild_id, decision, sha256, phash, dhash, ahash,
                           source_message_id, source_channel_id, source_jump_url,
                           file_path, file_size_bytes, created_at, moderator_id, active
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        sample["sample_id"],
                        sample["guild_id"],
                        sample["decision"],
                        sample["sha256"],
                        sample["phash"],
                        sample["dhash"],
                        sample["ahash"],
                        sample.get("source_message_id"),
                        sample.get("source_channel_id"),
                        sample.get("source_jump_url"),
                        sample.get("file_path"),
                        int(sample.get("file_size_bytes") or 0),
                        int(sample["created_at"]),
                        sample.get("moderator_id"),
                    ),
                )
                temp_path.replace(path)
                published = True
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                finally:
                    if published:
                        path.unlink(missing_ok=True)
                raise
            finally:
                temp_path.unlink(missing_ok=True)
        return "inserted"

    def rows(
        self,
        guild_id: int,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        where = "guild_id = ?" if include_inactive else "guild_id = ? AND active = 1"
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT sample_id, guild_id, decision, sha256, phash, dhash, ahash,
                       source_message_id, source_channel_id, source_jump_url,
                       file_path, file_size_bytes, created_at, moderator_id, active
                FROM imagescan_samples
                WHERE {where}
                ORDER BY created_at DESC
                """,
                (str(guild_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_file(
        self,
        guild_id: int,
        sample_id: str,
        file_path: str | None,
        file_size: int,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE imagescan_samples
                SET file_path = ?, file_size_bytes = ?
                WHERE guild_id = ? AND sample_id = ?
                """,
                (file_path, file_size, str(guild_id), sample_id),
            )

    def delete(self, guild_id: int, sample_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM imagescan_samples WHERE guild_id = ? AND sample_id = ?",
                (str(guild_id), sample_id),
            )

    def deactivate(self, guild_id: int, sample_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE imagescan_samples
                SET active = 0
                WHERE guild_id = ? AND sample_id = ?
                """,
                (str(guild_id), sample_id),
            )

    def export_rows(self, guild_id: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            events = connection.execute(
                """
                SELECT *
                FROM imagescan_events
                WHERE guild_id = ?
                ORDER BY created_at ASC
                """,
                (str(guild_id),),
            ).fetchall()
            files = connection.execute(
                """
                SELECT *
                FROM imagescan_files
                WHERE event_id IN (
                    SELECT event_id FROM imagescan_events WHERE guild_id = ?
                )
                ORDER BY event_id ASC, file_index ASC
                """,
                (str(guild_id),),
            ).fetchall()
        files_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in files:
            files_by_event[str(row["event_id"])].append(dict(row))
        rows: list[dict[str, Any]] = []
        for row in events:
            item = dict(row)
            item["files"] = files_by_event.get(str(row["event_id"]), [])
            rows.append(item)
        return rows

    def export_samples(self, guild_id: int) -> list[dict[str, Any]]:
        return self.rows(guild_id, include_inactive=True)
