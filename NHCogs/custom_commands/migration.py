from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .arguments import ArgumentSignatureError, argument_signature
from .catalog import (
    DELETED_USER_ID,
    DELETED_USER_NAME,
    MAX_RESPONSE_LENGTH,
    CommandEditor,
    CustomCommand,
    CustomCommandCatalog,
    CustomResponse,
    InvalidCommand,
)

LEGACY_DATETIME_FORMAT = "%d/%m/%Y %H:%M:%S"
LEGACY_RESPONSE_WEIGHT = 100
COOLDOWN_SCOPE_ALIASES = {"server": "guild", "user": "member"}
LEGACY_CONFIG_IDENTIFIER = 414589031223512


class LegacyCleanupError(RuntimeError):
    pass


class LegacyCleanupIncomplete(LegacyCleanupError):
    pass


class LegacyCleanupPreconditionError(LegacyCleanupError):
    pass


@dataclass(frozen=True)
class LegacyDataStatus:
    active_command_count: int
    legacy_command_count: int
    artifact_file_count: int
    artifact_bytes: int
    migration_state_present: bool

    @property
    def is_clean(self) -> bool:
        return (
            self.legacy_command_count == 0
            and self.artifact_file_count == 0
            and not self.migration_state_present
        )


def _legacy_command_count(guilds: Any) -> int:
    if not isinstance(guilds, Mapping):
        return 0
    count = 0
    for guild_data in guilds.values():
        if not isinstance(guild_data, Mapping):
            continue
        commands = guild_data.get("commands")
        if isinstance(commands, Mapping):
            count += len(commands)
    return count


def _migration_root(data_root: Path) -> Path:
    resolved_data_root = Path(data_root).resolve()
    resolved_migration_root = (resolved_data_root / "migration").resolve()
    if (
        resolved_migration_root.parent != resolved_data_root
        or resolved_migration_root.name != "migration"
    ):
        raise LegacyCleanupError("Migration artifact path escaped the Custom Commands data root")
    return resolved_migration_root


def _artifact_stats(data_root: Path) -> tuple[int, int]:
    root = _migration_root(data_root)
    if not root.exists():
        return 0, 0
    files = tuple(path for path in root.rglob("*") if path.is_file())
    return len(files), sum(path.stat().st_size for path in files)


def _sqlite_legacy_status(database_path: Path) -> tuple[int, bool]:
    path = Path(database_path)
    if not path.is_file():
        raise LegacyCleanupError("The active Custom Commands database does not exist")
    with closing(sqlite3.connect(path)) as connection:
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise LegacyCleanupError("The active Custom Commands database failed its integrity check")
        active_count = connection.execute(
            "SELECT COUNT(*) FROM custom_commands"
        ).fetchone()[0]
        migration_state_present = (
            connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type = 'table' AND name = 'custom_command_migration_state'"""
            ).fetchone()
            is not None
        )
    return int(active_count), migration_state_present


async def inspect_legacy_data(
    legacy_config: Any,
    data_root: Path,
    database_path: Path,
) -> LegacyDataStatus:
    guilds = await legacy_config.all_guilds()
    legacy_count = _legacy_command_count(guilds)
    artifact_count, artifact_bytes = await asyncio.to_thread(
        _artifact_stats,
        data_root,
    )
    active_count, migration_state_present = await asyncio.to_thread(
        _sqlite_legacy_status,
        database_path,
    )
    return LegacyDataStatus(
        active_command_count=active_count,
        legacy_command_count=legacy_count,
        artifact_file_count=artifact_count,
        artifact_bytes=artifact_bytes,
        migration_state_present=migration_state_present,
    )


def _delete_migration_artifacts(data_root: Path) -> None:
    root = _migration_root(data_root)
    if root.exists():
        shutil.rmtree(root)


def _drop_migration_state(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("DROP TABLE IF EXISTS custom_command_migration_state")


async def purge_legacy_data(
    legacy_config: Any,
    data_root: Path,
    database_path: Path,
) -> LegacyDataStatus:
    before = await inspect_legacy_data(legacy_config, data_root, database_path)
    if before.active_command_count == 0 and before.legacy_command_count > 0:
        raise LegacyCleanupPreconditionError(
            "The active catalog is empty while legacy CustomCom still contains commands"
        )
    await legacy_config.clear_all()
    await asyncio.to_thread(_delete_migration_artifacts, data_root)
    await asyncio.to_thread(_drop_migration_state, database_path)
    after = await inspect_legacy_data(legacy_config, data_root, database_path)
    if not after.is_clean:
        raise LegacyCleanupIncomplete(
            "Legacy CustomCom cleanup did not remove every target"
        )
    return after


def _redact_legacy_commands(commands: Any, user_id: int) -> int:
    if not isinstance(commands, dict):
        return 0
    changed = 0
    for record in commands.values():
        if not isinstance(record, dict):
            continue
        author = record.get("author")
        if isinstance(author, dict) and author.get("id") == user_id:
            author["id"] = DELETED_USER_ID
            author["name"] = DELETED_USER_NAME
            changed += 1
        editors = record.get("editors")
        if isinstance(editors, list) and user_id in editors:
            redacted = [DELETED_USER_ID if item == user_id else item for item in editors]
            record["editors"] = list(dict.fromkeys(redacted))
            changed += 1
    return changed


def _redact_report_target(report: Any, user_id: int) -> int:
    if not isinstance(report, dict) or not isinstance(report.get("target"), list):
        return 0
    changed = 0
    for command in report["target"]:
        if not isinstance(command, dict):
            continue
        author = command.get("author")
        if isinstance(author, dict) and author.get("id") == user_id:
            author["id"] = DELETED_USER_ID
            author["name"] = DELETED_USER_NAME
            changed += 1
        editors = command.get("editors")
        if not isinstance(editors, list):
            continue
        retained = []
        redacted_editor = None
        for editor in editors:
            if not isinstance(editor, dict) or editor.get("user_id") != user_id:
                retained.append(editor)
                continue
            redacted_editor = dict(editor)
            redacted_editor["user_id"] = DELETED_USER_ID
            redacted_editor["display_name"] = DELETED_USER_NAME
            changed += 1
        if redacted_editor is not None and not any(
            isinstance(editor, dict) and editor.get("user_id") == DELETED_USER_ID
            for editor in retained
        ):
            retained.append(redacted_editor)
        command["editors"] = retained
    if changed:
        report["privacy_redacted"] = True
    return changed


async def redact_legacy_config(config: Any, user_id: int) -> int:
    guilds = await config.all_guilds()
    changed = 0
    for raw_guild_id, guild_data in guilds.items():
        if not isinstance(guild_data, Mapping):
            continue
        commands = deepcopy(guild_data.get("commands", {}))
        guild_changes = _redact_legacy_commands(commands, user_id)
        if not guild_changes:
            continue
        await config.guild_from_id(int(raw_guild_id)).commands.set(commands)
        changed += guild_changes
    return changed


def redact_migration_artifacts(migration_root: Path, user_id: int) -> int:
    changed = 0
    for backup_path in migration_root.glob("*/legacy-backup.json"):
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        backup_changes = 0
        if isinstance(backup, dict):
            for guild_data in backup.values():
                if isinstance(guild_data, dict):
                    backup_changes += _redact_legacy_commands(
                        guild_data.get("commands", {}),
                        user_id,
                    )
        if backup_changes:
            backup_path.write_bytes(_pretty_json(backup))
            changed += backup_changes
    for report_path in migration_root.glob("*/migration-report.json"):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report_changes = _redact_report_target(report, user_id)
        if report_changes:
            report_path.write_bytes(_pretty_json(report))
            changed += report_changes
    return changed


async def redact_custom_command_user_data(
    catalog: Any,
    legacy_config: Any,
    migration_root: Path,
    user_id: int,
) -> None:
    await catalog.redact_user(user_id)
    await redact_legacy_config(legacy_config, user_id)
    await asyncio.to_thread(redact_migration_artifacts, migration_root, user_id)


@dataclass(frozen=True)
class MigrationIssue:
    guild_id: int
    command_name: str
    code: str
    message: str


@dataclass(frozen=True)
class MigrationPlan:
    commands: tuple[CustomCommand, ...]
    issues: tuple[MigrationIssue, ...]
    source_digest: str
    destination_digest: str
    backup_json: bytes
    report_json: bytes
    errors_text: bytes | None
    summary: Mapping[str, int]

    @property
    def can_apply(self) -> bool:
        return not self.issues


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _command_mapping(command: CustomCommand) -> dict[str, Any]:
    return {
        "guild_id": command.guild_id,
        "name": command.name,
        "author": {"id": command.author_id, "name": command.author_name},
        "created_at": command.created_at.astimezone(timezone.utc).isoformat(),
        "edited_at": (
            command.edited_at.astimezone(timezone.utc).isoformat()
            if command.edited_at is not None
            else None
        ),
        "revision": command.revision,
        "responses": [asdict(response) for response in command.responses],
        "cooldowns": dict(command.cooldowns),
        "editors": [
            {
                "user_id": editor.user_id,
                "display_name": editor.display_name,
                "first_edited_at": editor.first_edited_at.astimezone(
                    timezone.utc
                ).isoformat(),
                "last_edited_at": editor.last_edited_at.astimezone(
                    timezone.utc
                ).isoformat(),
            }
            for editor in command.editors
        ],
    }


def command_digest(commands: Iterable[CustomCommand]) -> str:
    canonical = [
        _command_mapping(command)
        for command in sorted(commands, key=lambda item: (item.guild_id, item.name))
    ]
    return _digest(_canonical_json(canonical))


class LegacyMigrationPlanner:
    """Validate the complete Red Config snapshot and build one canonical target."""

    def plan(  # noqa: PLR0912, PLR0915
        self,
        legacy_guilds: Mapping[int | str, Any],
        *,
        reserved_names: Iterable[str] = (),
    ) -> MigrationPlan:
        backup_json = _pretty_json(legacy_guilds)
        source_digest = _digest(_canonical_json(legacy_guilds))
        reserved = {name.casefold() for name in reserved_names}
        commands = []
        issues = []
        seen: set[tuple[int, str]] = set()
        simple_count = 0
        random_count = 0
        response_count = 0
        tombstone_count = 0
        legacy_record_count = 0
        author_metadata_count = 0
        editor_metadata_count = 0
        cooldown_counts = {"member": 0, "channel": 0, "guild": 0}

        for raw_guild_id, guild_data in sorted(
            legacy_guilds.items(),
            key=lambda item: int(item[0]),
        ):
            guild_id = int(raw_guild_id)
            if not isinstance(guild_data, Mapping):
                issues.append(
                    MigrationIssue(
                        guild_id,
                        "<guild>",
                        "malformed_guild",
                        "Guild custom command data is not a mapping",
                    )
                )
                continue
            legacy_commands = guild_data.get("commands", {})
            if not isinstance(legacy_commands, Mapping):
                issues.append(
                    MigrationIssue(
                        guild_id,
                        "<guild>",
                        "malformed_commands",
                        "Guild commands are not a mapping",
                    )
                )
                continue
            for raw_name, record in sorted(legacy_commands.items()):
                if not record:
                    tombstone_count += 1
                    continue
                legacy_record_count += 1
                if isinstance(record, Mapping):
                    author = record.get("author")
                    if (
                        isinstance(author, Mapping)
                        and type(author.get("id")) is int
                        and isinstance(author.get("name"), str)
                    ):
                        author_metadata_count += 1
                    editors = record.get("editors")
                    if isinstance(editors, list):
                        editor_metadata_count += sum(
                            type(editor_id) is int for editor_id in editors
                        )
                command, command_issues = self._convert_command(
                    guild_id,
                    raw_name,
                    record,
                    reserved,
                )
                issues.extend(command_issues)
                if command is None:
                    continue
                key = (guild_id, command.name)
                if key in seen:
                    issues.append(
                        MigrationIssue(
                            guild_id,
                            command.name,
                            "name_conflict",
                            "Multiple legacy records normalize to the same command name",
                        )
                    )
                    continue
                seen.add(key)
                commands.append(command)
                response_count += len(command.responses)
                if len(command.responses) == 1:
                    simple_count += 1
                else:
                    random_count += 1
                for scope in command.cooldowns:
                    cooldown_counts[scope] += 1

        destination_digest = command_digest(commands)
        conflicting_names = sum(issue.code == "name_conflict" for issue in issues)
        empty_responses = sum(
            issue.code in {"empty_response", "empty_responses"} for issue in issues
        )
        oversized_responses = sum(
            issue.code == "oversized_response" for issue in issues
        )
        summary = MappingProxyType(
            {
                "guilds": len(legacy_guilds),
                "legacy_records": legacy_record_count,
                "commands": len(commands),
                "commands_ready": len(commands),
                "simple_commands": simple_count,
                "random_commands": random_count,
                "responses": response_count,
                "inactive_tombstones": tombstone_count,
                "authors_with_metadata": author_metadata_count,
                "editor_ids": editor_metadata_count,
                "issues": len(issues),
                "name_conflicts": conflicting_names,
                "empty_responses": empty_responses,
                "oversized_responses": oversized_responses,
                "member_cooldowns": cooldown_counts["member"],
                "channel_cooldowns": cooldown_counts["channel"],
                "guild_cooldowns": cooldown_counts["guild"],
            }
        )
        report = {
            "source_digest": source_digest,
            "destination_digest": destination_digest,
            "summary": dict(summary),
            "issues": [asdict(issue) for issue in issues],
            "target": [_command_mapping(command) for command in commands],
        }
        errors_text = None
        if issues:
            errors_text = (
                "\n".join(
                    f"guild {issue.guild_id}, command {issue.command_name}: "
                    f"[{issue.code}] {issue.message}"
                    for issue in issues
                )
                + "\n"
            ).encode("utf-8")
        return MigrationPlan(
            commands=tuple(commands),
            issues=tuple(issues),
            source_digest=source_digest,
            destination_digest=destination_digest,
            backup_json=backup_json,
            report_json=_pretty_json(report),
            errors_text=errors_text,
            summary=summary,
        )

    def _convert_command(
        self,
        guild_id: int,
        raw_name: Any,
        record: Any,
        reserved: set[str],
    ) -> tuple[CustomCommand | None, tuple[MigrationIssue, ...]]:
        display_name = str(raw_name)
        issues = []
        if not isinstance(record, Mapping):
            return None, (
                MigrationIssue(
                    guild_id,
                    display_name,
                    "malformed_record",
                    "Command data is not a mapping",
                ),
            )
        try:
            name = CustomCommandCatalog.normalize_name(display_name)
        except InvalidCommand as error:
            issues.append(
                MigrationIssue(
                    guild_id,
                    display_name,
                    "invalid_name",
                    str(error),
                )
            )
            name = display_name.casefold()
        if name in reserved:
            issues.append(
                MigrationIssue(
                    guild_id,
                    name,
                    "name_conflict",
                    "Command name conflicts with an active bot command",
                )
            )
        stored_name = record.get("command")
        if not isinstance(stored_name, str) or stored_name.casefold() != name:
            issues.append(
                MigrationIssue(
                    guild_id,
                    name,
                    "malformed_command_name",
                    "Stored command name does not match its Config key",
                )
            )

        author = record.get("author")
        if not isinstance(author, Mapping):
            issues.append(
                MigrationIssue(
                    guild_id,
                    name,
                    "missing_author",
                    "Author metadata is missing",
                )
            )
            author_id = 0
            author_name = "Unknown"
        else:
            raw_author_id = author.get("id")
            raw_author_name = author.get("name")
            if type(raw_author_id) is not int or not isinstance(raw_author_name, str):
                issues.append(
                    MigrationIssue(
                        guild_id,
                        name,
                        "malformed_author",
                        "Author ID or name is malformed",
                    )
                )
                author_id = 0
                author_name = "Unknown"
            else:
                author_id = raw_author_id
                author_name = raw_author_name

        created_at = self._parse_datetime(
            record.get("created_at"),
            guild_id=guild_id,
            command_name=name,
            field="created_at",
            issues=issues,
            required=True,
        )
        edited_at = self._parse_datetime(
            record.get("edited_at"),
            guild_id=guild_id,
            command_name=name,
            field="edited_at",
            issues=issues,
            required=False,
        )
        responses = self._convert_responses(
            guild_id,
            name,
            record.get("response"),
            issues,
        )
        cooldowns = self._convert_cooldowns(
            guild_id,
            name,
            record.get("cooldowns", {}),
            issues,
        )
        editors = self._convert_editors(
            guild_id,
            name,
            record.get("editors", []),
            edited_at or created_at,
            issues,
        )
        if issues or created_at is None:
            return None, tuple(issues)
        return (
            CustomCommand(
                guild_id=guild_id,
                name=name,
                author_id=author_id,
                author_name=author_name,
                created_at=created_at,
                edited_at=edited_at,
                revision=1,
                responses=responses,
                cooldowns=MappingProxyType(cooldowns),
                editors=editors,
            ),
            (),
        )

    @staticmethod
    def _parse_datetime(
        value: Any,
        *,
        guild_id: int,
        command_name: str,
        field: str,
        issues: list[MigrationIssue],
        required: bool,
    ) -> datetime | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            issues.append(
                MigrationIssue(
                    guild_id,
                    command_name,
                    f"malformed_{field}",
                    f"{field} is missing or malformed",
                )
            )
            return None
        try:
            parsed = datetime.strptime(value, LEGACY_DATETIME_FORMAT)
        except ValueError:
            issues.append(
                MigrationIssue(
                    guild_id,
                    command_name,
                    f"malformed_{field}",
                    f"{field} does not use Red's timestamp format",
                )
            )
            return None
        return parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _convert_responses(
        guild_id: int,
        command_name: str,
        value: Any,
        issues: list[MigrationIssue],
    ) -> tuple[CustomResponse, ...]:
        raw_responses = [value] if isinstance(value, str) else value
        if not isinstance(raw_responses, list) or not raw_responses:
            issues.append(
                MigrationIssue(
                    guild_id,
                    command_name,
                    "empty_responses",
                    "Command has no responses",
                )
            )
            return ()
        responses = []
        signatures = []
        for index, content in enumerate(raw_responses):
            if not isinstance(content, str):
                issues.append(
                    MigrationIssue(
                        guild_id,
                        command_name,
                        "malformed_response",
                        f"Response {index + 1} is not text",
                    )
                )
                continue
            if not content.strip():
                issues.append(
                    MigrationIssue(
                        guild_id,
                        command_name,
                        "empty_response",
                        f"Response {index + 1} is empty",
                    )
                )
                continue
            if len(content) > MAX_RESPONSE_LENGTH:
                issues.append(
                    MigrationIssue(
                        guild_id,
                        command_name,
                        "oversized_response",
                        f"Response {index + 1} exceeds Discord's message limit",
                    )
                )
                continue
            try:
                signatures.append(argument_signature(content))
            except ArgumentSignatureError as error:
                issues.append(
                    MigrationIssue(
                        guild_id,
                        command_name,
                        "invalid_arguments",
                        f"Response {index + 1}: {error}",
                    )
                )
                continue
            response_id = hashlib.sha256(
                f"{guild_id}\0{command_name}\0{index}\0{content}".encode()
            ).hexdigest()
            responses.append(
                CustomResponse(
                    response_id=response_id,
                    display_order=index,
                    content=content,
                    weight=LEGACY_RESPONSE_WEIGHT,
                )
            )
        if signatures and any(signature != signatures[0] for signature in signatures[1:]):
            issues.append(
                MigrationIssue(
                    guild_id,
                    command_name,
                    "inconsistent_arguments",
                    "Random responses use different argument signatures",
                )
            )
        return tuple(responses)

    @staticmethod
    def _convert_cooldowns(
        guild_id: int,
        command_name: str,
        value: Any,
        issues: list[MigrationIssue],
    ) -> dict[str, int]:
        if not isinstance(value, Mapping):
            issues.append(
                MigrationIssue(
                    guild_id,
                    command_name,
                    "malformed_cooldowns",
                    "Cooldowns are not a mapping",
                )
            )
            return {}
        cooldowns = {}
        for raw_scope, seconds in value.items():
            scope = COOLDOWN_SCOPE_ALIASES.get(str(raw_scope), str(raw_scope))
            if scope not in {"member", "channel", "guild"}:
                issues.append(
                    MigrationIssue(
                        guild_id,
                        command_name,
                        "invalid_cooldown_scope",
                        f"Unknown cooldown scope: {raw_scope}",
                    )
                )
                continue
            if type(seconds) is not int or seconds <= 0:
                issues.append(
                    MigrationIssue(
                        guild_id,
                        command_name,
                        "invalid_cooldown",
                        f"Cooldown {scope} is not a positive whole number",
                    )
                )
                continue
            cooldowns[scope] = seconds
        return cooldowns

    @staticmethod
    def _convert_editors(
        guild_id: int,
        command_name: str,
        value: Any,
        edited_at: datetime | None,
        issues: list[MigrationIssue],
    ) -> tuple[CommandEditor, ...]:
        if not isinstance(value, list):
            issues.append(
                MigrationIssue(
                    guild_id,
                    command_name,
                    "malformed_editors",
                    "Editors are not a list",
                )
            )
            return ()
        if value and edited_at is None:
            issues.append(
                MigrationIssue(
                    guild_id,
                    command_name,
                    "missing_edited_at",
                    "Editor metadata exists without an edit timestamp",
                )
            )
            return ()
        if edited_at is None:
            return ()
        unique = []
        seen = set()
        for user_id in value:
            if type(user_id) is not int:
                issues.append(
                    MigrationIssue(
                        guild_id,
                        command_name,
                        "malformed_editor",
                        "An editor ID is malformed",
                    )
                )
                continue
            if user_id in seen:
                continue
            seen.add(user_id)
            unique.append(
                CommandEditor(
                    user_id=user_id,
                    display_name=str(user_id),
                    first_edited_at=edited_at,
                    last_edited_at=edited_at,
                )
            )
        return tuple(
            sorted(unique, key=lambda editor: (editor.first_edited_at, editor.user_id))
        )
