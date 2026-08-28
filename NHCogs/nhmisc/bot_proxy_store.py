from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_CHARACTER_NAME_LENGTH = 80
MESSAGE_TRANSITION_TIMEOUT_SECONDS = 5 * 60
ALLOWED_AVATAR_MEDIA_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)


class CharacterStoreError(ValueError):
    pass


class CharacterExists(CharacterStoreError):
    pass


class CharacterNotFound(CharacterStoreError):
    pass


class InvalidCharacter(CharacterStoreError):
    pass


class StaleCharacterRevision(CharacterStoreError):
    pass


class StaleMessageRevision(ValueError):
    pass


class MessageNotFound(ValueError):
    pass


class ProxySender(str, Enum):
    BOT = "bot"
    CHARACTER = "character"


@dataclass(frozen=True, slots=True)
class CharacterPreset:
    guild_id: int
    preset_name: str
    display_name: str
    avatar_bytes: bytes | None
    avatar_media_type: str | None
    avatar_sha256: str | None
    revision: int
    created_by: int
    updated_by: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ActiveSessionRecord:
    session_id: str
    guild_id: int
    moderator_id: int
    launcher_channel_id: int
    launcher_message_id: int
    thread_id: int
    dashboard_message_id: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ProxyMessageRecord:
    guild_id: int
    channel_id: int
    message_id: int
    moderator_id: int
    sender: ProxySender
    webhook_id: int | None
    content: str
    original_content: str
    reply_message_id: int | None
    character_preset_name: str | None
    character_display_name: str | None
    avatar_sha256: str | None
    revision: int
    created_at: datetime
    edited_by: int | None = None
    edited_at: datetime | None = None
    deleted_by: int | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProxyMessageEvent:
    guild_id: int
    channel_id: int
    message_id: int
    revision: int
    action: str
    content: str
    moderator_id: int
    created_at: datetime


def _clean_name(value: str, *, field: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise InvalidCharacter(f"{field} cannot be empty")
    if len(cleaned) > MAX_CHARACTER_NAME_LENGTH:
        raise InvalidCharacter(
            f"{field} cannot exceed {MAX_CHARACTER_NAME_LENGTH} characters"
        )
    return cleaned


def _avatar_digest(avatar_bytes: bytes | None) -> str | None:
    if avatar_bytes is None:
        return None
    return hashlib.sha256(avatar_bytes).hexdigest()


def _validate_avatar(
    avatar_bytes: bytes | None, avatar_media_type: str | None
) -> None:
    if avatar_bytes is None:
        if avatar_media_type is not None:
            raise InvalidCharacter("avatar media type requires avatar data")
        return
    if avatar_media_type not in ALLOWED_AVATAR_MEDIA_TYPES:
        raise InvalidCharacter("unsupported avatar media type")
    if len(avatar_bytes) > MAX_AVATAR_BYTES:
        raise InvalidCharacter(f"avatar cannot exceed {MAX_AVATAR_BYTES} bytes")


class BotProxyStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    async def create_character(
        self,
        *,
        guild_id: int,
        preset_name: str,
        display_name: str,
        avatar_bytes: bytes | None,
        avatar_media_type: str | None,
        moderator_id: int,
    ) -> CharacterPreset:
        cleaned_preset_name = _clean_name(preset_name, field="preset name")
        cleaned_display_name = _clean_name(display_name, field="display name")
        _validate_avatar(avatar_bytes, avatar_media_type)
        async with self._lock:
            return await asyncio.to_thread(
                self._create_character_sync,
                guild_id=guild_id,
                preset_name=cleaned_preset_name,
                display_name=cleaned_display_name,
                avatar_bytes=avatar_bytes,
                avatar_media_type=avatar_media_type,
                moderator_id=moderator_id,
            )

    async def get_character(
        self, guild_id: int, preset_name: str
    ) -> CharacterPreset | None:
        name_key = _clean_name(preset_name, field="preset name").casefold()
        async with self._lock:
            return await asyncio.to_thread(self._get_character_sync, guild_id, name_key)

    async def list_characters(self, guild_id: int) -> tuple[CharacterPreset, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_characters_sync, guild_id)

    async def update_character(
        self,
        *,
        guild_id: int,
        preset_name: str,
        expected_revision: int,
        new_preset_name: str,
        display_name: str,
        avatar_bytes: bytes | None,
        avatar_media_type: str | None,
        moderator_id: int,
    ) -> CharacterPreset:
        name_key = _clean_name(preset_name, field="preset name").casefold()
        cleaned_new_name = _clean_name(new_preset_name, field="preset name")
        cleaned_display_name = _clean_name(display_name, field="display name")
        _validate_avatar(avatar_bytes, avatar_media_type)
        async with self._lock:
            return await asyncio.to_thread(
                self._update_character_sync,
                guild_id=guild_id,
                name_key=name_key,
                expected_revision=expected_revision,
                new_preset_name=cleaned_new_name,
                display_name=cleaned_display_name,
                avatar_bytes=avatar_bytes,
                avatar_media_type=avatar_media_type,
                moderator_id=moderator_id,
            )

    async def delete_character(
        self, *, guild_id: int, preset_name: str, expected_revision: int
    ) -> CharacterPreset:
        name_key = _clean_name(preset_name, field="preset name").casefold()
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_character_sync, guild_id, name_key, expected_revision
            )

    async def record_active_session(self, session: ActiveSessionRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._record_active_session_sync, session)

    async def list_active_sessions(self) -> tuple[ActiveSessionRecord, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_active_sessions_sync)

    async def remove_active_session(self, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._remove_active_session_sync, session_id)

    async def remember_webhook(
        self, guild_id: int, parent_channel_id: int, webhook_id: int
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._remember_webhook_sync,
                guild_id,
                parent_channel_id,
                webhook_id,
            )

    async def get_webhook_id(
        self, guild_id: int, parent_channel_id: int
    ) -> int | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_webhook_id_sync, guild_id, parent_channel_id
            )

    async def forget_webhook(self, guild_id: int, parent_channel_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._forget_webhook_sync, guild_id, parent_channel_id
            )

    async def record_message(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        moderator_id: int,
        sender: ProxySender,
        webhook_id: int | None,
        content: str,
        reply_message_id: int | None,
        character_preset_name: str | None,
        character_display_name: str | None,
        avatar_sha256: str | None,
    ) -> ProxyMessageRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._record_message_sync,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                moderator_id=moderator_id,
                sender=sender,
                webhook_id=webhook_id,
                content=content,
                reply_message_id=reply_message_id,
                character_preset_name=character_preset_name,
                character_display_name=character_display_name,
                avatar_sha256=avatar_sha256,
            )

    async def edit_message(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        expected_revision: int,
        content: str,
        moderator_id: int,
        transition_token: str,
    ) -> ProxyMessageRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._edit_message_sync,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                expected_revision=expected_revision,
                content=content,
                moderator_id=moderator_id,
                transition_token=transition_token,
            )

    async def mark_message_deleted(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        expected_revision: int,
        moderator_id: int,
        transition_token: str,
    ) -> ProxyMessageRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._mark_message_deleted_sync,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                expected_revision=expected_revision,
                moderator_id=moderator_id,
                transition_token=transition_token,
            )

    async def claim_message_transition(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        expected_revision: int,
    ) -> str:
        token = uuid4().hex
        async with self._lock:
            await asyncio.to_thread(
                self._claim_message_transition_sync,
                guild_id,
                channel_id,
                message_id,
                expected_revision,
                token,
            )
        return token

    async def release_message_transition(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        transition_token: str,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._release_message_transition_sync,
                guild_id,
                channel_id,
                message_id,
                transition_token,
            )

    async def get_message(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> ProxyMessageRecord | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_message_sync, guild_id, channel_id, message_id
            )

    async def list_message_events(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> tuple[ProxyMessageEvent, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_message_events_sync,
                guild_id,
                channel_id,
                message_id,
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_proxy_characters (
                    guild_id INTEGER NOT NULL,
                    name_key TEXT NOT NULL,
                    preset_name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    avatar_bytes BLOB,
                    avatar_media_type TEXT,
                    avatar_sha256 TEXT,
                    revision INTEGER NOT NULL,
                    created_by INTEGER NOT NULL,
                    updated_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, name_key)
                );

                CREATE INDEX IF NOT EXISTS bot_proxy_characters_display_order
                ON bot_proxy_characters (guild_id, name_key);

                CREATE TABLE IF NOT EXISTS bot_proxy_active_sessions (
                    session_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    launcher_channel_id INTEGER NOT NULL,
                    launcher_message_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL UNIQUE,
                    dashboard_message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS bot_proxy_sessions_by_moderator
                ON bot_proxy_active_sessions (guild_id, moderator_id, created_at);

                CREATE TABLE IF NOT EXISTS bot_proxy_webhooks (
                    guild_id INTEGER NOT NULL,
                    parent_channel_id INTEGER NOT NULL,
                    webhook_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, parent_channel_id)
                );

                CREATE TABLE IF NOT EXISTS bot_proxy_messages (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    sender TEXT NOT NULL CHECK (sender IN ('bot', 'character')),
                    webhook_id INTEGER,
                    content TEXT NOT NULL,
                    original_content TEXT NOT NULL,
                    reply_message_id INTEGER,
                    character_preset_name TEXT,
                    character_display_name TEXT,
                    avatar_sha256 TEXT,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    edited_by INTEGER,
                    edited_at TEXT,
                    deleted_by INTEGER,
                    deleted_at TEXT,
                    transition_token TEXT,
                    transition_started_at TEXT,
                    PRIMARY KEY (guild_id, channel_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS bot_proxy_message_events (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('sent', 'edited', 'deleted')),
                    content TEXT NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id, message_id, revision)
                );
                """
            )
            connection.commit()

    def _create_character_sync(
        self,
        *,
        guild_id: int,
        preset_name: str,
        display_name: str,
        avatar_bytes: bytes | None,
        avatar_media_type: str | None,
        moderator_id: int,
    ) -> CharacterPreset:
        now = datetime.now(timezone.utc)
        name_key = preset_name.casefold()
        avatar_sha256 = _avatar_digest(avatar_bytes)
        with self._connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO bot_proxy_characters (
                        guild_id, name_key, preset_name, display_name,
                        avatar_bytes, avatar_media_type, avatar_sha256,
                        revision, created_by, updated_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        name_key,
                        preset_name,
                        display_name,
                        avatar_bytes,
                        avatar_media_type,
                        avatar_sha256,
                        moderator_id,
                        moderator_id,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CharacterExists(preset_name) from error
            connection.commit()
            row = connection.execute(
                """
                SELECT * FROM bot_proxy_characters
                WHERE guild_id = ? AND name_key = ?
                """,
                (guild_id, name_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("created character could not be read")
        return self._character_from_row(row)

    def _get_character_sync(
        self, guild_id: int, name_key: str
    ) -> CharacterPreset | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM bot_proxy_characters
                WHERE guild_id = ? AND name_key = ?
                """,
                (guild_id, name_key),
            ).fetchone()
        return self._character_from_row(row) if row is not None else None

    def _list_characters_sync(self, guild_id: int) -> tuple[CharacterPreset, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bot_proxy_characters
                WHERE guild_id = ?
                ORDER BY name_key
                """,
                (guild_id,),
            ).fetchall()
        return tuple(self._character_from_row(row) for row in rows)

    def _update_character_sync(
        self,
        *,
        guild_id: int,
        name_key: str,
        expected_revision: int,
        new_preset_name: str,
        display_name: str,
        avatar_bytes: bytes | None,
        avatar_media_type: str | None,
        moderator_id: int,
    ) -> CharacterPreset:
        updated_at = datetime.now(timezone.utc)
        new_name_key = new_preset_name.casefold()
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT revision FROM bot_proxy_characters
                WHERE guild_id = ? AND name_key = ?
                """,
                (guild_id, name_key),
            ).fetchone()
            if existing is None:
                raise CharacterNotFound(name_key)
            if existing["revision"] != expected_revision:
                raise StaleCharacterRevision(name_key)
            try:
                cursor = connection.execute(
                    """
                    UPDATE bot_proxy_characters
                    SET name_key = ?, preset_name = ?, display_name = ?,
                        avatar_bytes = ?, avatar_media_type = ?, avatar_sha256 = ?,
                        revision = revision + 1, updated_by = ?, updated_at = ?
                    WHERE guild_id = ? AND name_key = ? AND revision = ?
                    """,
                    (
                        new_name_key,
                        new_preset_name,
                        display_name,
                        avatar_bytes,
                        avatar_media_type,
                        _avatar_digest(avatar_bytes),
                        moderator_id,
                        updated_at.isoformat(),
                        guild_id,
                        name_key,
                        expected_revision,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CharacterExists(new_preset_name) from error
            if cursor.rowcount != 1:
                raise StaleCharacterRevision(name_key)
            connection.commit()
            row = connection.execute(
                """
                SELECT * FROM bot_proxy_characters
                WHERE guild_id = ? AND name_key = ?
                """,
                (guild_id, new_name_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("updated character could not be read")
        return self._character_from_row(row)

    def _delete_character_sync(
        self, guild_id: int, name_key: str, expected_revision: int
    ) -> CharacterPreset:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM bot_proxy_characters
                WHERE guild_id = ? AND name_key = ?
                """,
                (guild_id, name_key),
            ).fetchone()
            if row is None:
                raise CharacterNotFound(name_key)
            character = self._character_from_row(row)
            if character.revision != expected_revision:
                raise StaleCharacterRevision(name_key)
            cursor = connection.execute(
                """
                DELETE FROM bot_proxy_characters
                WHERE guild_id = ? AND name_key = ? AND revision = ?
                """,
                (guild_id, name_key, expected_revision),
            )
            if cursor.rowcount != 1:
                raise StaleCharacterRevision(name_key)
            connection.commit()
        return character

    def _record_active_session_sync(self, session: ActiveSessionRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_proxy_active_sessions (
                    session_id, guild_id, moderator_id, launcher_channel_id,
                    launcher_message_id, thread_id, dashboard_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.guild_id,
                    session.moderator_id,
                    session.launcher_channel_id,
                    session.launcher_message_id,
                    session.thread_id,
                    session.dashboard_message_id,
                    session.created_at.isoformat(),
                ),
            )
            connection.commit()

    def _list_active_sessions_sync(self) -> tuple[ActiveSessionRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bot_proxy_active_sessions
                ORDER BY created_at, session_id
                """
            ).fetchall()
        return tuple(
            ActiveSessionRecord(
                session_id=row["session_id"],
                guild_id=row["guild_id"],
                moderator_id=row["moderator_id"],
                launcher_channel_id=row["launcher_channel_id"],
                launcher_message_id=row["launcher_message_id"],
                thread_id=row["thread_id"],
                dashboard_message_id=row["dashboard_message_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    def _remove_active_session_sync(self, session_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM bot_proxy_active_sessions WHERE session_id = ?",
                (session_id,),
            )
            connection.commit()

    def _remember_webhook_sync(
        self, guild_id: int, parent_channel_id: int, webhook_id: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_proxy_webhooks (guild_id, parent_channel_id, webhook_id)
                VALUES (?, ?, ?)
                ON CONFLICT (guild_id, parent_channel_id)
                DO UPDATE SET webhook_id = excluded.webhook_id
                """,
                (guild_id, parent_channel_id, webhook_id),
            )
            connection.commit()

    def _get_webhook_id_sync(
        self, guild_id: int, parent_channel_id: int
    ) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT webhook_id FROM bot_proxy_webhooks
                WHERE guild_id = ? AND parent_channel_id = ?
                """,
                (guild_id, parent_channel_id),
            ).fetchone()
        return row["webhook_id"] if row is not None else None

    def _forget_webhook_sync(self, guild_id: int, parent_channel_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM bot_proxy_webhooks
                WHERE guild_id = ? AND parent_channel_id = ?
                """,
                (guild_id, parent_channel_id),
            )
            connection.commit()

    def _record_message_sync(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        moderator_id: int,
        sender: ProxySender,
        webhook_id: int | None,
        content: str,
        reply_message_id: int | None,
        character_preset_name: str | None,
        character_display_name: str | None,
        avatar_sha256: str | None,
    ) -> ProxyMessageRecord:
        created_at = datetime.now(timezone.utc)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_proxy_messages (
                    guild_id, channel_id, message_id, moderator_id, sender,
                    webhook_id, content, original_content, reply_message_id, character_preset_name,
                    character_display_name, avatar_sha256, revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    message_id,
                    moderator_id,
                    sender.value,
                    webhook_id,
                    content,
                    content,
                    reply_message_id,
                    character_preset_name,
                    character_display_name,
                    avatar_sha256,
                    created_at.isoformat(),
                ),
            )
            self._insert_message_event(
                connection,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                revision=1,
                action="sent",
                content=content,
                moderator_id=moderator_id,
                created_at=created_at,
            )
            connection.commit()
            row = self._select_message(connection, guild_id, channel_id, message_id)
        if row is None:
            raise RuntimeError("recorded Bot Proxy message could not be read")
        return self._message_from_row(row)

    def _edit_message_sync(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        expected_revision: int,
        content: str,
        moderator_id: int,
        transition_token: str,
    ) -> ProxyMessageRecord:
        edited_at = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = self._select_message(connection, guild_id, channel_id, message_id)
            row = self._require_current_message(row, expected_revision)
            if row["transition_token"] != transition_token:
                raise StaleMessageRevision(message_id)
            if row["deleted_at"] is not None:
                raise MessageNotFound(message_id)
            cursor = connection.execute(
                """
                UPDATE bot_proxy_messages
                SET content = ?, revision = revision + 1,
                    edited_by = ?, edited_at = ?,
                    transition_token = NULL, transition_started_at = NULL
                WHERE guild_id = ? AND channel_id = ? AND message_id = ?
                    AND revision = ? AND deleted_at IS NULL
                    AND transition_token = ?
                """,
                (
                    content,
                    moderator_id,
                    edited_at.isoformat(),
                    guild_id,
                    channel_id,
                    message_id,
                    expected_revision,
                    transition_token,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleMessageRevision(message_id)
            self._insert_message_event(
                connection,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                revision=expected_revision + 1,
                action="edited",
                content=content,
                moderator_id=moderator_id,
                created_at=edited_at,
            )
            connection.commit()
            updated = self._select_message(connection, guild_id, channel_id, message_id)
        if updated is None:
            raise RuntimeError("edited Bot Proxy message could not be read")
        return self._message_from_row(updated)

    def _mark_message_deleted_sync(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        expected_revision: int,
        moderator_id: int,
        transition_token: str,
    ) -> ProxyMessageRecord:
        deleted_at = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = self._select_message(connection, guild_id, channel_id, message_id)
            row = self._require_current_message(row, expected_revision)
            if row["transition_token"] != transition_token:
                raise StaleMessageRevision(message_id)
            if row["deleted_at"] is not None:
                raise MessageNotFound(message_id)
            cursor = connection.execute(
                """
                UPDATE bot_proxy_messages
                SET revision = revision + 1, deleted_by = ?, deleted_at = ?,
                    transition_token = NULL, transition_started_at = NULL
                WHERE guild_id = ? AND channel_id = ? AND message_id = ?
                    AND revision = ? AND deleted_at IS NULL
                    AND transition_token = ?
                """,
                (
                    moderator_id,
                    deleted_at.isoformat(),
                    guild_id,
                    channel_id,
                    message_id,
                    expected_revision,
                    transition_token,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleMessageRevision(message_id)
            self._insert_message_event(
                connection,
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                revision=expected_revision + 1,
                action="deleted",
                content=row["content"],
                moderator_id=moderator_id,
                created_at=deleted_at,
            )
            connection.commit()
            updated = self._select_message(connection, guild_id, channel_id, message_id)
        if updated is None:
            raise RuntimeError("deleted Bot Proxy message could not be read")
        return self._message_from_row(updated)

    def _get_message_sync(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> ProxyMessageRecord | None:
        with self._connection() as connection:
            row = self._select_message(connection, guild_id, channel_id, message_id)
        return self._message_from_row(row) if row is not None else None

    def _claim_message_transition_sync(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        expected_revision: int,
        token: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        stale_before = now.timestamp() - MESSAGE_TRANSITION_TIMEOUT_SECONDS
        stale_iso = datetime.fromtimestamp(stale_before, timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE bot_proxy_messages
                SET transition_token = ?, transition_started_at = ?
                WHERE guild_id = ? AND channel_id = ? AND message_id = ?
                    AND revision = ? AND deleted_at IS NULL
                    AND (
                        transition_token IS NULL
                        OR transition_started_at < ?
                    )
                """,
                (
                    token,
                    now.isoformat(),
                    guild_id,
                    channel_id,
                    message_id,
                    expected_revision,
                    stale_iso,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleMessageRevision(message_id)
            connection.commit()

    def _release_message_transition_sync(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        transition_token: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE bot_proxy_messages
                SET transition_token = NULL, transition_started_at = NULL
                WHERE guild_id = ? AND channel_id = ? AND message_id = ?
                    AND transition_token = ?
                """,
                (guild_id, channel_id, message_id, transition_token),
            )
            connection.commit()

    def _list_message_events_sync(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> tuple[ProxyMessageEvent, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bot_proxy_message_events
                WHERE guild_id = ? AND channel_id = ? AND message_id = ?
                ORDER BY revision
                """,
                (guild_id, channel_id, message_id),
            ).fetchall()
        return tuple(
            ProxyMessageEvent(
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                message_id=row["message_id"],
                revision=row["revision"],
                action=row["action"],
                content=row["content"],
                moderator_id=row["moderator_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _insert_message_event(
        connection: sqlite3.Connection,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        revision: int,
        action: str,
        content: str,
        moderator_id: int,
        created_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO bot_proxy_message_events (
                guild_id, channel_id, message_id, revision, action,
                content, moderator_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                message_id,
                revision,
                action,
                content,
                moderator_id,
                created_at.isoformat(),
            ),
        )

    @staticmethod
    def _select_message(
        connection: sqlite3.Connection,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM bot_proxy_messages
            WHERE guild_id = ? AND channel_id = ? AND message_id = ?
            """,
            (guild_id, channel_id, message_id),
        ).fetchone()

    @staticmethod
    def _require_current_message(
        row: sqlite3.Row | None, expected_revision: int
    ) -> sqlite3.Row:
        if row is None:
            raise MessageNotFound
        if row["revision"] != expected_revision:
            raise StaleMessageRevision(row["message_id"])
        return row

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> ProxyMessageRecord:
        return ProxyMessageRecord(
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            moderator_id=row["moderator_id"],
            sender=ProxySender(row["sender"]),
            webhook_id=row["webhook_id"],
            content=row["content"],
            original_content=row["original_content"],
            reply_message_id=row["reply_message_id"],
            character_preset_name=row["character_preset_name"],
            character_display_name=row["character_display_name"],
            avatar_sha256=row["avatar_sha256"],
            revision=row["revision"],
            created_at=datetime.fromisoformat(row["created_at"]),
            edited_by=row["edited_by"],
            edited_at=(
                datetime.fromisoformat(row["edited_at"])
                if row["edited_at"] is not None
                else None
            ),
            deleted_by=row["deleted_by"],
            deleted_at=(
                datetime.fromisoformat(row["deleted_at"])
                if row["deleted_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _character_from_row(row: sqlite3.Row) -> CharacterPreset:
        return CharacterPreset(
            guild_id=row["guild_id"],
            preset_name=row["preset_name"],
            display_name=row["display_name"],
            avatar_bytes=row["avatar_bytes"],
            avatar_media_type=row["avatar_media_type"],
            avatar_sha256=row["avatar_sha256"],
            revision=row["revision"],
            created_by=row["created_by"],
            updated_by=row["updated_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
