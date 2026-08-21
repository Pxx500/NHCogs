from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from .arguments import ArgumentSignatureError, argument_signature
from .migration_state import MigrationPhase

MAX_NAME_LENGTH = 100
MAX_RESPONSE_LENGTH = 2_000
MIN_WEIGHT = 1
MAX_WEIGHT = 1_000
COOLDOWN_SCOPES = frozenset(("member", "channel", "guild"))
DELETED_USER_ID = 0xDE1
DELETED_USER_NAME = "Deleted User"
COMMAND_NAME_PATTERN = re.compile(r"^[^\s]+$")


class CatalogError(Exception):
    pass


class CommandExists(CatalogError):
    pass


class CommandNotFound(CatalogError):
    pass


class StaleRevision(CatalogError):
    pass


class InvalidCommand(CatalogError):
    pass


@dataclass(frozen=True)
class ResponseDraft:
    content: str
    weight: int = 100
    response_id: str | None = None


@dataclass(frozen=True)
class CustomResponse:
    response_id: str
    display_order: int
    content: str
    weight: int


@dataclass(frozen=True)
class CommandEditor:
    user_id: int
    display_name: str
    first_edited_at: datetime
    last_edited_at: datetime


@dataclass(frozen=True)
class CustomCommand:
    guild_id: int
    name: str
    author_id: int
    author_name: str
    created_at: datetime
    edited_at: datetime | None
    revision: int
    responses: tuple[CustomResponse, ...]
    cooldowns: Mapping[str, int]
    editors: tuple[CommandEditor, ...]


def _to_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _from_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _required_timestamp(value: str | None, field: str) -> datetime:
    parsed = _from_timestamp(value)
    if parsed is None:
        raise RuntimeError(f"Custom command {field} timestamp is missing")
    return parsed


class CustomCommandCatalog:
    """Authoritative transaction owner for custom command definitions."""

    def __init__(self, database_path: Path):
        self._database_path = Path(database_path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_sync(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS custom_commands (
                       guild_id INTEGER NOT NULL,
                       name TEXT NOT NULL,
                       author_id INTEGER NOT NULL,
                       author_name TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       edited_at TEXT,
                       revision INTEGER NOT NULL,
                       PRIMARY KEY (guild_id, name)
                   );
                   CREATE TABLE IF NOT EXISTS custom_command_responses (
                       response_id TEXT PRIMARY KEY,
                       guild_id INTEGER NOT NULL,
                       command_name TEXT NOT NULL,
                       display_order INTEGER NOT NULL,
                       content TEXT NOT NULL,
                       weight INTEGER NOT NULL,
                       UNIQUE (guild_id, command_name, display_order),
                       FOREIGN KEY (guild_id, command_name)
                           REFERENCES custom_commands(guild_id, name)
                           ON DELETE CASCADE
                   );
                   CREATE TABLE IF NOT EXISTS custom_command_cooldowns (
                       guild_id INTEGER NOT NULL,
                       command_name TEXT NOT NULL,
                       scope TEXT NOT NULL,
                       seconds INTEGER NOT NULL,
                       PRIMARY KEY (guild_id, command_name, scope),
                       FOREIGN KEY (guild_id, command_name)
                           REFERENCES custom_commands(guild_id, name)
                           ON DELETE CASCADE
                   );
                   CREATE TABLE IF NOT EXISTS custom_command_editors (
                       guild_id INTEGER NOT NULL,
                       command_name TEXT NOT NULL,
                       user_id INTEGER NOT NULL,
                       display_name TEXT NOT NULL,
                       first_edited_at TEXT NOT NULL,
                       last_edited_at TEXT NOT NULL,
                       PRIMARY KEY (guild_id, command_name, user_id),
                       FOREIGN KEY (guild_id, command_name)
                           REFERENCES custom_commands(guild_id, name)
                           ON DELETE CASCADE
                   );"""
            )

    @staticmethod
    def normalize_name(name: str) -> str:
        normalized = name.strip().casefold()
        if (
            not normalized
            or len(normalized) > MAX_NAME_LENGTH
            or COMMAND_NAME_PATTERN.fullmatch(normalized) is None
        ):
            raise InvalidCommand("Command names must be one non-empty word")
        return normalized

    @staticmethod
    def _validated_cooldowns(
        cooldowns: Mapping[str, int] | None,
    ) -> dict[str, int]:
        validated: dict[str, int] = {}
        for scope, seconds in (cooldowns or {}).items():
            normalized_scope = scope.casefold()
            if normalized_scope not in COOLDOWN_SCOPES:
                raise InvalidCommand(f"Unknown cooldown scope: {scope}")
            if type(seconds) is not int or seconds <= 0:
                raise InvalidCommand("Cooldowns must be positive whole seconds")
            validated[normalized_scope] = seconds
        return validated

    @staticmethod
    def _validated_responses(
        responses: Sequence[ResponseDraft],
    ) -> tuple[CustomResponse, ...]:
        if not responses:
            raise InvalidCommand("At least one response is required")
        validated = []
        signatures = []
        response_ids: set[str] = set()
        for display_order, response in enumerate(responses):
            if not response.content.strip():
                raise InvalidCommand("Responses cannot be empty")
            if len(response.content) > MAX_RESPONSE_LENGTH:
                raise InvalidCommand("Responses cannot be longer than 2000 characters")
            if type(response.weight) is not int or not MIN_WEIGHT <= response.weight <= MAX_WEIGHT:
                raise InvalidCommand("Response weights must be whole numbers from 1 to 1000")
            try:
                signatures.append(argument_signature(response.content))
            except ArgumentSignatureError as error:
                raise InvalidCommand(str(error)) from error
            response_id = response.response_id or str(uuid4())
            if response_id in response_ids:
                raise InvalidCommand("Response IDs must be unique")
            response_ids.add(response_id)
            validated.append(
                CustomResponse(
                    response_id=response_id,
                    display_order=display_order,
                    content=response.content,
                    weight=response.weight,
                )
            )
        if any(signature != signatures[0] for signature in signatures[1:]):
            raise InvalidCommand("Every response must use the same arguments")
        return tuple(validated)

    async def create(
        self,
        *,
        guild_id: int,
        name: str,
        author_id: int,
        author_name: str,
        responses: Sequence[ResponseDraft],
        cooldowns: Mapping[str, int] | None = None,
        created_at: datetime | None = None,
    ) -> CustomCommand:
        normalized = self.normalize_name(name)
        validated_responses = self._validated_responses(responses)
        validated_cooldowns = self._validated_cooldowns(cooldowns)
        created = created_at or datetime.now(timezone.utc)
        return await asyncio.to_thread(
            self._create_sync,
            guild_id=guild_id,
            name=normalized,
            author_id=author_id,
            author_name=author_name,
            responses=validated_responses,
            cooldowns=validated_cooldowns,
            created_at=created,
        )

    def _create_sync(
        self,
        *,
        guild_id: int,
        name: str,
        author_id: int,
        author_name: str,
        responses: tuple[CustomResponse, ...],
        cooldowns: Mapping[str, int],
        created_at: datetime,
    ) -> CustomCommand:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._command_exists(connection, guild_id, name):
                raise CommandExists(name)
            self._insert_command(
                connection,
                guild_id=guild_id,
                name=name,
                author_id=author_id,
                author_name=author_name,
                created_at=created_at,
                edited_at=None,
                revision=1,
                responses=responses,
                cooldowns=cooldowns,
                editors=(),
            )
            return self._read_command(connection, guild_id, name)

    @staticmethod
    def _command_exists(
        connection: sqlite3.Connection, guild_id: int, name: str
    ) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM custom_commands WHERE guild_id = ? AND name = ?",
                (guild_id, name),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _insert_responses(
        connection: sqlite3.Connection,
        guild_id: int,
        name: str,
        responses: Sequence[CustomResponse],
    ) -> None:
        connection.executemany(
            """INSERT INTO custom_command_responses
               (response_id, guild_id, command_name, display_order, content, weight)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                (
                    response.response_id,
                    guild_id,
                    name,
                    response.display_order,
                    response.content,
                    response.weight,
                )
                for response in responses
            ),
        )

    @staticmethod
    def _insert_cooldowns(
        connection: sqlite3.Connection,
        guild_id: int,
        name: str,
        cooldowns: Mapping[str, int],
    ) -> None:
        connection.executemany(
            """INSERT INTO custom_command_cooldowns
               (guild_id, command_name, scope, seconds) VALUES (?, ?, ?, ?)""",
            ((guild_id, name, scope, seconds) for scope, seconds in cooldowns.items()),
        )

    @staticmethod
    def _insert_editors(
        connection: sqlite3.Connection,
        guild_id: int,
        name: str,
        editors: Sequence[CommandEditor],
    ) -> None:
        connection.executemany(
            """INSERT INTO custom_command_editors
               (guild_id, command_name, user_id, display_name,
                first_edited_at, last_edited_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                (
                    guild_id,
                    name,
                    editor.user_id,
                    editor.display_name,
                    _to_timestamp(editor.first_edited_at),
                    _to_timestamp(editor.last_edited_at),
                )
                for editor in editors
            ),
        )

    def _insert_command(
        self,
        connection: sqlite3.Connection,
        *,
        guild_id: int,
        name: str,
        author_id: int,
        author_name: str,
        created_at: datetime,
        edited_at: datetime | None,
        revision: int,
        responses: Sequence[CustomResponse],
        cooldowns: Mapping[str, int],
        editors: Sequence[CommandEditor],
    ) -> None:
        connection.execute(
            """INSERT INTO custom_commands
               (guild_id, name, author_id, author_name, created_at, edited_at, revision)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                name,
                author_id,
                author_name,
                _to_timestamp(created_at),
                _to_timestamp(edited_at) if edited_at is not None else None,
                revision,
            ),
        )
        self._insert_responses(connection, guild_id, name, responses)
        self._insert_cooldowns(connection, guild_id, name, cooldowns)
        self._insert_editors(connection, guild_id, name, editors)

    def _read_command(
        self, connection: sqlite3.Connection, guild_id: int, name: str
    ) -> CustomCommand:
        row = connection.execute(
            "SELECT * FROM custom_commands WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        ).fetchone()
        if row is None:
            raise CommandNotFound(name)
        responses = tuple(
            CustomResponse(
                response_id=response["response_id"],
                display_order=response["display_order"],
                content=response["content"],
                weight=response["weight"],
            )
            for response in connection.execute(
                """SELECT * FROM custom_command_responses
                   WHERE guild_id = ? AND command_name = ? ORDER BY display_order""",
                (guild_id, name),
            )
        )
        cooldowns = MappingProxyType(
            {
                cooldown["scope"]: cooldown["seconds"]
                for cooldown in connection.execute(
                    """SELECT * FROM custom_command_cooldowns
                       WHERE guild_id = ? AND command_name = ? ORDER BY scope""",
                    (guild_id, name),
                )
            }
        )
        editors = tuple(
            CommandEditor(
                user_id=editor["user_id"],
                display_name=editor["display_name"],
                first_edited_at=_required_timestamp(
                    editor["first_edited_at"], "first edit"
                ),
                last_edited_at=_required_timestamp(
                    editor["last_edited_at"], "last edit"
                ),
            )
            for editor in connection.execute(
                """SELECT * FROM custom_command_editors
                   WHERE guild_id = ? AND command_name = ? ORDER BY first_edited_at, user_id""",
                (guild_id, name),
            )
        )
        created_at = _required_timestamp(row["created_at"], "creation")
        return CustomCommand(
            guild_id=row["guild_id"],
            name=row["name"],
            author_id=row["author_id"],
            author_name=row["author_name"],
            created_at=created_at,
            edited_at=_from_timestamp(row["edited_at"]),
            revision=row["revision"],
            responses=responses,
            cooldowns=cooldowns,
            editors=editors,
        )

    async def get(self, guild_id: int, name: str) -> CustomCommand | None:
        normalized = self.normalize_name(name)
        return await asyncio.to_thread(self._get_sync, guild_id, normalized)

    def _get_sync(self, guild_id: int, name: str) -> CustomCommand | None:
        with closing(self._connect()) as connection:
            if not self._command_exists(connection, guild_id, name):
                return None
            return self._read_command(connection, guild_id, name)

    async def list_commands(self, guild_id: int) -> tuple[CustomCommand, ...]:
        return await asyncio.to_thread(self._list_commands_sync, guild_id)

    def _list_commands_sync(self, guild_id: int) -> tuple[CustomCommand, ...]:
        with closing(self._connect()) as connection:
            names = tuple(
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM custom_commands WHERE guild_id = ? ORDER BY name",
                    (guild_id,),
                )
            )
            return tuple(self._read_command(connection, guild_id, name) for name in names)

    async def edit(
        self,
        *,
        guild_id: int,
        name: str,
        expected_revision: int,
        editor_id: int,
        editor_name: str,
        responses: Sequence[ResponseDraft] | None = None,
        cooldowns: Mapping[str, int] | None = None,
        edited_at: datetime | None = None,
    ) -> CustomCommand:
        normalized = self.normalize_name(name)
        validated_responses = (
            self._validated_responses(responses) if responses is not None else None
        )
        validated_cooldowns = (
            self._validated_cooldowns(cooldowns) if cooldowns is not None else None
        )
        return await asyncio.to_thread(
            self._edit_sync,
            guild_id=guild_id,
            name=normalized,
            expected_revision=expected_revision,
            editor_id=editor_id,
            editor_name=editor_name,
            responses=validated_responses,
            cooldowns=validated_cooldowns,
            edited_at=edited_at or datetime.now(timezone.utc),
        )

    def _edit_sync(
        self,
        *,
        guild_id: int,
        name: str,
        expected_revision: int,
        editor_id: int,
        editor_name: str,
        responses: tuple[CustomResponse, ...] | None,
        cooldowns: Mapping[str, int] | None,
        edited_at: datetime,
    ) -> CustomCommand:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT revision FROM custom_commands WHERE guild_id = ? AND name = ?",
                (guild_id, name),
            ).fetchone()
            if current is None:
                raise CommandNotFound(name)
            if current["revision"] != expected_revision:
                raise StaleRevision(name)
            connection.execute(
                """UPDATE custom_commands SET edited_at = ?, revision = revision + 1
                   WHERE guild_id = ? AND name = ?""",
                (_to_timestamp(edited_at), guild_id, name),
            )
            if responses is not None:
                connection.execute(
                    "DELETE FROM custom_command_responses WHERE guild_id = ? AND command_name = ?",
                    (guild_id, name),
                )
                self._insert_responses(connection, guild_id, name, responses)
            if cooldowns is not None:
                connection.execute(
                    "DELETE FROM custom_command_cooldowns WHERE guild_id = ? AND command_name = ?",
                    (guild_id, name),
                )
                self._insert_cooldowns(connection, guild_id, name, cooldowns)
            timestamp = _to_timestamp(edited_at)
            connection.execute(
                """INSERT INTO custom_command_editors
                   (guild_id, command_name, user_id, display_name,
                    first_edited_at, last_edited_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, command_name, user_id) DO UPDATE SET
                       display_name = excluded.display_name,
                       last_edited_at = excluded.last_edited_at""",
                (guild_id, name, editor_id, editor_name, timestamp, timestamp),
            )
            return self._read_command(connection, guild_id, name)

    async def delete(
        self, *, guild_id: int, name: str, expected_revision: int
    ) -> None:
        normalized = self.normalize_name(name)
        await asyncio.to_thread(
            self._delete_sync,
            guild_id,
            normalized,
            expected_revision,
        )

    def _delete_sync(self, guild_id: int, name: str, expected_revision: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM custom_commands WHERE guild_id = ? AND name = ?",
                (guild_id, name),
            ).fetchone()
            if row is None:
                raise CommandNotFound(name)
            if row["revision"] != expected_revision:
                raise StaleRevision(name)
            connection.execute(
                "DELETE FROM custom_commands WHERE guild_id = ? AND name = ?",
                (guild_id, name),
            )

    async def redact_user(self, user_id: int) -> int:
        return await asyncio.to_thread(self._redact_user_sync, user_id)

    def _redact_user_sync(self, user_id: int) -> int:
        with closing(self._connect()) as connection, connection:
            author_result = connection.execute(
                """UPDATE custom_commands SET author_id = ?, author_name = ?
                   WHERE author_id = ?""",
                (DELETED_USER_ID, DELETED_USER_NAME, user_id),
            )
            editor_rows = tuple(
                connection.execute(
                    "SELECT * FROM custom_command_editors WHERE user_id = ?",
                    (user_id,),
                )
            )
            for editor in editor_rows:
                existing = connection.execute(
                    """SELECT * FROM custom_command_editors
                       WHERE guild_id = ? AND command_name = ? AND user_id = ?""",
                    (
                        editor["guild_id"],
                        editor["command_name"],
                        DELETED_USER_ID,
                    ),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """UPDATE custom_command_editors
                           SET user_id = ?, display_name = ?
                           WHERE guild_id = ? AND command_name = ? AND user_id = ?""",
                        (
                            DELETED_USER_ID,
                            DELETED_USER_NAME,
                            editor["guild_id"],
                            editor["command_name"],
                            user_id,
                        ),
                    )
                    continue
                connection.execute(
                    """UPDATE custom_command_editors
                       SET display_name = ?, first_edited_at = ?, last_edited_at = ?
                       WHERE guild_id = ? AND command_name = ? AND user_id = ?""",
                    (
                        DELETED_USER_NAME,
                        min(existing["first_edited_at"], editor["first_edited_at"]),
                        max(existing["last_edited_at"], editor["last_edited_at"]),
                        editor["guild_id"],
                        editor["command_name"],
                        DELETED_USER_ID,
                    ),
                )
                connection.execute(
                    """DELETE FROM custom_command_editors
                       WHERE guild_id = ? AND command_name = ? AND user_id = ?""",
                    (editor["guild_id"], editor["command_name"], user_id),
                )
            return author_result.rowcount + len(editor_rows)

    async def import_all(self, commands: Sequence[CustomCommand]) -> None:
        await asyncio.to_thread(self._import_all_sync, tuple(commands), None)

    async def import_migration(
        self,
        commands: Sequence[CustomCommand],
        *,
        source_digest: str,
        destination_digest: str,
    ) -> None:
        await asyncio.to_thread(
            self._import_all_sync,
            tuple(commands),
            (source_digest, destination_digest),
        )

    def _import_all_sync(
        self,
        commands: tuple[CustomCommand, ...],
        migration_digests: tuple[str, str] | None,
    ) -> None:
        validated = []
        seen: set[tuple[int, str]] = set()
        for command in commands:
            name = self.normalize_name(command.name)
            key = (command.guild_id, name)
            if key in seen:
                raise InvalidCommand(f"Duplicate imported command: {name}")
            seen.add(key)
            responses = self._validated_responses(
                tuple(
                    ResponseDraft(
                        content=response.content,
                        weight=response.weight,
                        response_id=response.response_id,
                    )
                    for response in command.responses
                )
            )
            cooldowns = self._validated_cooldowns(command.cooldowns)
            expected = CustomCommand(
                guild_id=command.guild_id,
                name=name,
                author_id=command.author_id,
                author_name=command.author_name,
                created_at=command.created_at,
                edited_at=command.edited_at,
                revision=command.revision,
                responses=responses,
                cooldowns=MappingProxyType(cooldowns),
                editors=command.editors,
            )
            validated.append(expected)
        expected_commands = tuple(
            sorted(validated, key=lambda command: (command.guild_id, command.name))
        )
        if expected_commands != tuple(
            sorted(commands, key=lambda command: (command.guild_id, command.name))
        ):
            raise RuntimeError("Imported custom commands changed during validation")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if migration_digests is not None:
                source_digest, _destination_digest = migration_digests
                state = connection.execute(
                    """SELECT phase, source_digest
                       FROM custom_command_migration_state WHERE singleton = 1"""
                ).fetchone()
                if (
                    state is None
                    or state["phase"] != MigrationPhase.PLANNED.value
                    or state["source_digest"] != source_digest
                ):
                    raise RuntimeError("Migration plan state changed before import")
                existing_count = connection.execute(
                    "SELECT COUNT(*) FROM custom_commands"
                ).fetchone()[0]
                if existing_count:
                    raise RuntimeError("Migration destination is not empty")
            for command in validated:
                if self._command_exists(connection, command.guild_id, command.name):
                    raise CommandExists(command.name)
                self._insert_command(
                    connection,
                    guild_id=command.guild_id,
                    name=command.name,
                    author_id=command.author_id,
                    author_name=command.author_name,
                    created_at=command.created_at,
                    edited_at=command.edited_at,
                    revision=command.revision,
                    responses=command.responses,
                    cooldowns=command.cooldowns,
                    editors=command.editors,
                )
            if migration_digests is None:
                stored = tuple(
                    self._read_command(connection, command.guild_id, command.name)
                    for command in validated
                )
                expected_stored = tuple(validated)
            else:
                imported_keys = connection.execute(
                    "SELECT guild_id, name FROM custom_commands ORDER BY guild_id, name"
                ).fetchall()
                stored = tuple(
                    self._read_command(connection, row["guild_id"], row["name"])
                    for row in imported_keys
                )
                expected_stored = expected_commands
            if stored != expected_stored:
                raise RuntimeError("Imported custom commands failed read-back verification")
            if migration_digests is not None:
                source_digest, destination_digest = migration_digests
                connection.execute(
                    """UPDATE custom_command_migration_state
                       SET phase = ?, source_digest = ?, destination_digest = ?,
                           updated_at = ? WHERE singleton = 1""",
                    (
                        MigrationPhase.IMPORTED_NOT_ACTIVE.value,
                        source_digest,
                        destination_digest,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
