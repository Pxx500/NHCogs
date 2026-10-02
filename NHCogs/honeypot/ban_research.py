"""Export historical ban observations from Red and ExtendedModLog messages.

This reads log messages, not Discord's current ban list. Role deltas only establish
observed roles, never a complete member snapshot. Raw embeds retain the evidence
needed to check derived values or extend the supported logger formats.
"""

import asyncio
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

_BAN_CASE = re.compile(r"^Case #(\d+)\s*\|\s*Ban$", re.IGNORECASE)
_SUBJECT = re.compile(
    r"\((\d{15,22})\)(?:\s+(?:has joined the guild|has left the guild|updated))?$"
)
_ROLE_CHANGE = re.compile(r"<@!?(\d{15,22})> had the <@&(\d{15,22})> role (applied|removed)\.")
_WRITE_BATCH_SIZE = 250


@dataclass(frozen=True)
class BanResearchExport:
    archives: tuple[Path, ...]
    account_count: int
    ban_count: int
    member_event_count: int


def _json_line(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _append_records(path: Path, records: list[dict]) -> None:
    with path.open("ab") as stream:
        for record in records:
            stream.write(_json_line(record))


def _record(message, channel_id: int) -> dict:
    return {
        "message_id": str(message.id),
        "channel_id": str(channel_id),
        "author_id": str(message.author.id),
        "message_created_at": message.created_at.isoformat(),
        "url": message.jump_url,
        "content": message.content,
        "embeds": [embed.to_dict() for embed in message.embeds],
    }


def _event_time(embed: dict, record: dict) -> tuple[str, str]:
    timestamp = embed.get("timestamp")
    if timestamp:
        try:
            date = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if date.tzinfo is not None:
                return date.astimezone(timezone.utc).isoformat(), "embed_timestamp"
        except (TypeError, ValueError):
            pass
    return record["message_created_at"], "message_created_at"


def _subject_id(embed: dict) -> str | None:
    # Moderator fields and mentions elsewhere must never identify the subject.
    for field in embed.get("fields", []):
        if field.get("name", "").strip().casefold() == "member id":
            value = field.get("value", "").strip("` \n")
            if re.fullmatch(r"\d{15,22}", value):
                return value
    match = _SUBJECT.search(embed.get("author", {}).get("name", ""))
    return match[1] if match else None


def _observation(embed: dict, record: dict, index: int) -> dict:
    event_at, basis = _event_time(embed, record)
    return {
        "event_at": event_at,
        "time_basis": basis,
        "message_created_at": record["message_created_at"],
        "message_id": record["message_id"],
        "url": record["url"],
        "embed_index": index,
        "author": embed.get("author", {}),
    }


async def _collect_bans(channel, path: Path, *, bot_id: int, cutoff: datetime):
    bans = defaultdict(list)
    seen = set()
    pending = []
    scanned = unmatched = 0
    async for message in channel.history(limit=None, oldest_first=True, before=cutoff):
        scanned += 1
        if message.author.id != bot_id:
            continue
        record = _record(message, channel.id)
        matched = False
        for index, embed in enumerate(record["embeds"]):
            case = _BAN_CASE.fullmatch(embed.get("title", ""))
            if not case:
                continue
            matched = True
            user_id = _subject_id(embed)
            if user_id is None:
                unmatched += 1
                continue
            key = (user_id, case[1])
            if key in seen:
                continue
            seen.add(key)
            ban = _observation(embed, record, index)
            ban.update(
                {
                    "case_number": int(case[1]),
                    "kind": "Ban",
                    "fields": embed.get("fields", []),
                }
            )
            bans[user_id].append(ban)
        if matched:
            pending.append(record)
        if len(pending) >= _WRITE_BATCH_SIZE:
            await asyncio.to_thread(_append_records, path, pending)
            pending = []
    await asyncio.to_thread(_append_records, path, pending)
    return bans, scanned, unmatched


async def _collect_members(channel, path: Path, users: set[str], *, bot_id: int, cutoff: datetime):
    events = defaultdict(list)
    pending = []
    scanned = count = 0
    async for message in channel.history(limit=None, oldest_first=True, before=cutoff):
        scanned += 1
        if message.author.id != bot_id:
            continue
        record = _record(message, channel.id)
        matched = False
        for index, embed in enumerate(record["embeds"]):
            user_id = _subject_id(embed)
            if user_id not in users:
                continue
            matched = True
            event = _observation(embed, record, index)
            author = embed.get("author", {}).get("name", "")
            event.update(
                {
                    "joined": author.endswith("has joined the guild"),
                    "roles_added": [],
                    "roles_removed": [],
                    "fields": embed.get("fields", []),
                }
            )
            for subject, role, action in _ROLE_CHANGE.findall(embed.get("description", "")):
                if subject == user_id:
                    event["roles_added" if action == "applied" else "roles_removed"].append(role)
            events[user_id].append(event)
            count += 1
        if matched:
            pending.append(record)
        if len(pending) >= _WRITE_BATCH_SIZE:
            await asyncio.to_thread(_append_records, path, pending)
            pending = []
    await asyncio.to_thread(_append_records, path, pending)
    return events, scanned, count


def _order(event: dict):
    return datetime.fromisoformat(event["event_at"]), int(event["message_id"]), event["embed_index"]


def _write_accounts(path: Path, bans: dict, events: dict) -> None:
    with path.open("wb") as stream:
        for user_id, user_bans in bans.items():
            member_events = sorted(events.get(user_id, []), key=_order)
            roles = set()
            joined_at = None
            observation = None
            position = 0
            for ban in sorted(user_bans, key=_order):
                while position < len(member_events) and _order(member_events[position]) <= _order(
                    ban
                ):
                    event = member_events[position]
                    if event["joined"]:
                        roles.clear()
                        joined_at = event["event_at"]
                    roles.update(event["roles_added"])
                    roles.difference_update(event["roles_removed"])
                    observation = event
                    position += 1
                ban.update(
                    {
                        "known_role_ids_at_ban": sorted(roles, key=int),
                        "role_snapshot_complete": False,
                        "joined_at": joined_at,
                        "last_member_observation": observation,
                    }
                )
            stream.write(_json_line({"user_id": user_id, "bans": sorted(user_bans, key=_order)}))


def _pack(directory: Path, files: list[Path], upload_limit: int) -> tuple[Path, ...]:
    archive_path = directory / "ban-research.zip"
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.name)
    if archive_path.stat().st_size <= upload_limit:
        return (archive_path,)
    archive_path.unlink()

    # Split on JSONL boundaries. Matching filenames in successive archives are
    # consecutive chunks to concatenate, not independent full datasets.
    budget = int(upload_limit * 0.98) - 2048
    if budget <= 0:
        raise ValueError("Upload limit is too small for the research export")
    archives = []
    entries = []
    size = 0

    def flush():
        nonlocal entries, size
        path = directory / f"ban-research-{len(archives) + 1:03}.zip"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        if path.stat().st_size > upload_limit:
            raise ValueError("Research archive exceeds the upload limit")
        archives.append(path)
        entries, size = [], 0

    for path in files:
        chunk = bytearray()
        with path.open("rb") as stream:
            for line in stream:
                if len(line) > budget:
                    raise ValueError("One research record exceeds the upload limit")
                if size + len(chunk) + len(line) > budget:
                    if chunk:
                        entries.append((path.name, bytes(chunk)))
                        chunk.clear()
                    flush()
                chunk.extend(line)
        entries.append((path.name, bytes(chunk)))
        size += len(chunk)
    if entries:
        flush()
    return tuple(archives)


async def export_ban_research(
    moderation_channel,
    member_channel,
    output_directory: Path,
    *,
    bot_id: int,
    upload_limit: int,
    cutoff: datetime,
) -> BanResearchExport:
    """Read each source once and write ZIP parts into a caller-owned temp directory.

    The caller owns authorization, private output and cleanup. Read or packaging
    errors propagate rather than presenting a partial scan as a complete export.
    """
    moderation_path = output_directory / "moderation-events.jsonl"
    member_path = output_directory / "member-events.jsonl"
    current_role_labels = {str(role.id): role.name for role in moderation_channel.guild.roles}
    role_labels_observed_at = datetime.now(timezone.utc).isoformat()
    bans, moderation_scanned, unmatched = await _collect_bans(
        moderation_channel,
        moderation_path,
        bot_id=bot_id,
        cutoff=cutoff,
    )
    events, member_scanned, member_count = await _collect_members(
        member_channel,
        member_path,
        set(bans),
        bot_id=bot_id,
        cutoff=cutoff,
    )
    accounts_path = output_directory / "accounts.jsonl"
    await asyncio.to_thread(_write_accounts, accounts_path, bans, events)
    ban_count = sum(len(values) for values in bans.values())
    metadata_path = output_directory / "metadata.json"
    metadata = {
        "format_version": 1,
        "cutoff": cutoff.isoformat(),
        "bot_id": str(bot_id),
        "moderation_channel_id": str(moderation_channel.id),
        "member_channel_id": str(member_channel.id),
        "moderation_messages_scanned": moderation_scanned,
        "member_messages_scanned": member_scanned,
        "account_count": len(bans),
        "ban_count": ban_count,
        "member_event_count": member_count,
        "ban_embeds_without_subject_id": unmatched,
        "current_role_labels": current_role_labels,
        "role_labels_observed_at": role_labels_observed_at,
        "limitations": [
            "Only this bot's Red ban logs and matching member logs are recognized",
            "Only normal Ban cases are included, not current ban status",
            "Role lists contain observed deltas only and may be incomplete",
            "Embed timestamps are logger observations, not guaranteed punishment effect times",
            "Names and avatar URLs are historical log observations, not current profiles",
            "Role labels come from the current guild cache, not historical role names",
            "Concatenate matching JSONL filenames in archive order when the export has multiple parts",
        ],
    }
    await asyncio.to_thread(metadata_path.write_bytes, _json_line(metadata))
    archives = await asyncio.to_thread(
        _pack,
        output_directory,
        [metadata_path, accounts_path, moderation_path, member_path],
        upload_limit,
    )
    return BanResearchExport(archives, len(bans), ban_count, member_count)
