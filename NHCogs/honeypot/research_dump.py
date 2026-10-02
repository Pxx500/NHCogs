"""Dump channel history for offline research without interpreting log messages."""

import asyncio
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from zipfile import ZIP_DEFLATED, ZipFile

import aiohttp

_WRITE_BATCH_SIZE = 250
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
_RETRY_BASE_SECONDS = 5
_RETRY_MAX_SECONDS = 60
_RETRY_MAX_EXPONENT = 4
_FILE_CHUNK_SIZE = 64 * 1024


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise asyncio.CancelledError


async def _file_operation(function, *args, cancel: Event):
    worker = asyncio.create_task(asyncio.to_thread(function, *args, cancel))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel.set()
        await asyncio.gather(worker, return_exceptions=True)
        raise


def _retry_delay(error: Exception, attempts: int) -> float | None:
    status = getattr(error, "status", None)
    retry_after = getattr(error, "retry_after", None)
    if (
        status is None
        and retry_after is None
        and not isinstance(
            error, (aiohttp.ClientConnectionError, asyncio.TimeoutError, TimeoutError)
        )
    ):
        return None
    if status is not None and status not in _RETRYABLE_HTTP_STATUSES:
        return None
    if retry_after is None:
        response = getattr(error, "response", None)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            delay = float(retry_after)
            if math.isfinite(delay) and delay > 0:
                return delay
        except (TypeError, ValueError):
            pass
    return min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * 2 ** min(attempts, _RETRY_MAX_EXPONENT))


@dataclass
class DumpProgress:
    """Live observations for the command UI. The exporter owns count updates."""

    phase: str = "moderation"
    counts: dict[str, int] = field(default_factory=lambda: {"moderation": 0, "members": 0})
    latest_message_at: datetime | None = None
    retry_until: float | None = None


@dataclass(frozen=True)
class ChannelDump:
    archives: tuple[Path, ...]
    moderation_messages: int
    member_messages: int
    failures: tuple[tuple[int, Exception], ...] = ()

    @property
    def complete(self) -> bool:
        return not self.failures


def _json_line(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _message_record(message, channel_id: int) -> dict:
    reference = message.reference
    return {
        "message_id": str(message.id),
        "channel_id": str(channel_id),
        "message_created_at": message.created_at.isoformat(),
        "message_edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "message_type": message.type.value,
        "author": {
            "id": str(message.author.id),
            "name": message.author.name,
            "display_name": message.author.display_name,
            "bot": message.author.bot,
        },
        "content": message.content,
        "embeds": [embed.to_dict() for embed in message.embeds],
        "attachments": [
            {
                "id": str(attachment.id),
                "filename": attachment.filename,
                "size": attachment.size,
                "content_type": attachment.content_type,
                "url": attachment.url,
                "proxy_url": attachment.proxy_url,
            }
            for attachment in message.attachments
        ],
        "reference": None
        if reference is None
        else {
            "message_id": str(reference.message_id) if reference.message_id else None,
            "channel_id": str(reference.channel_id),
            "guild_id": str(reference.guild_id) if reference.guild_id else None,
        },
        "url": message.jump_url,
    }


def _append_records(path: Path, records: list[dict], cancel: Event) -> None:
    with path.open("ab") as stream:
        for record in records:
            _check_cancel(cancel)
            stream.write(_json_line(record))


async def _collect_channel(
    channel, path: Path, category: str, progress: DumpProgress, *, cutoff: datetime, cancel: Event
) -> Exception | None:
    progress.phase = category
    progress.latest_message_at = None
    pending = []
    last_message = None
    attempts = 0
    while True:
        options = {"limit": None, "oldest_first": True, "before": cutoff}
        if last_message is not None:
            options["after"] = last_message
        try:
            async for message in channel.history(**options):
                if last_message is not None and message.id <= last_message.id:
                    continue
                pending.append(_message_record(message, channel.id))
                progress.counts[category] += 1
                progress.latest_message_at = message.created_at
                last_message = message
                attempts = 0
                if len(pending) >= _WRITE_BATCH_SIZE:
                    await _file_operation(_append_records, path, pending, cancel=cancel)
                    pending = []
        except Exception as error:
            delay = _retry_delay(error, attempts)
            if delay is None:
                if getattr(error, "status", None) is None:
                    raise
                await _file_operation(_append_records, path, pending, cancel=cancel)
                return error
            await _file_operation(_append_records, path, pending, cancel=cancel)
            pending = []
            progress.retry_until = asyncio.get_running_loop().time() + delay
            try:
                await asyncio.sleep(delay)
            finally:
                progress.retry_until = None
            attempts += 1
            continue
        await _file_operation(_append_records, path, pending, cancel=cancel)
        return None


def _archive_entry(archive: ZipFile, name: str, chunks, cancel: Event) -> None:
    with archive.open(name, "w", force_zip64=True) as target:
        for chunk in chunks:
            _check_cancel(cancel)
            target.write(chunk)


def _pack(directory: Path, files: list[Path], upload_limit: int, cancel: Event) -> tuple[Path, ...]:
    archive_path = directory / "research-dump.zip"
    with ZipFile(archive_path, "w", ZIP_DEFLATED) as archive:
        for path in files:
            with path.open("rb") as stream:
                _archive_entry(
                    archive, path.name, iter(lambda: stream.read(_FILE_CHUNK_SIZE), b""), cancel
                )
    _check_cancel(cancel)
    if archive_path.stat().st_size <= upload_limit:
        return (archive_path,)
    archive_path.unlink()
    budget = int(upload_limit * 0.98) - 2048
    if budget <= 0:
        raise ValueError("Upload limit is too small for the research dump")
    archives = []
    entries = []
    size = 0

    def flush():
        nonlocal entries, size
        path = directory / f"research-dump-{len(archives) + 1:03}.zip"
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, data in entries:
                view = memoryview(data)
                chunks = (
                    view[offset : offset + _FILE_CHUNK_SIZE]
                    for offset in range(0, len(view), _FILE_CHUNK_SIZE)
                )
                _archive_entry(archive, name, chunks, cancel)
        if path.stat().st_size > upload_limit:
            raise ValueError("Research archive exceeds the upload limit")
        archives.append(path)
        entries, size = [], 0

    for path in files:
        chunk = bytearray()
        with path.open("rb") as stream:
            for line in stream:
                _check_cancel(cancel)
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


async def dump_channels(
    moderation_channel,
    member_channel,
    output_directory: Path,
    *,
    bot_id: int,
    upload_limit: int,
    cutoff: datetime,
    progress: DumpProgress | None = None,
) -> ChannelDump:
    """Read each source once. The caller owns private delivery and temp cleanup."""
    progress = progress if progress is not None else DumpProgress()
    cancel = Event()
    role_labels = {str(role.id): role.name for role in moderation_channel.guild.roles}
    role_labels_at = datetime.now(timezone.utc).isoformat()
    files = []
    failures = []
    for category, channel, filename in (
        ("moderation", moderation_channel, "moderation-messages.jsonl"),
        ("members", member_channel, "member-messages.jsonl"),
    ):
        path = output_directory / filename
        failure = await _collect_channel(channel, path, category, progress, cutoff=cutoff, cancel=cancel)
        if failure is not None:
            failures.append((channel.id, failure))
        files.append(path)
    progress.phase = "packaging"
    metadata = {
        "format_version": 1,
        "export_type": "channel_history_dump",
        "complete": not failures,
        "channel_errors": [
            {
                "channel_id": str(channel_id),
                "status": error.status,
                "error_type": type(error).__name__,
            }
            for channel_id, error in failures
        ],
        "cutoff": cutoff.isoformat(),
        "bot_id": str(bot_id),
        "moderation_channel_id": str(moderation_channel.id),
        "moderation_channel_name": moderation_channel.name,
        "member_channel_id": str(member_channel.id),
        "member_channel_name": member_channel.name,
        "moderation_messages": progress.counts["moderation"],
        "member_messages": progress.counts["members"],
        "current_role_labels": role_labels,
        "role_labels_observed_at": role_labels_at,
        "limitations": [
            "No ban, account or role interpretation is performed",
            "Message authors are observed at export time, embedded log identities remain unchanged",
            "Attachments are metadata and URLs only, files and avatars are not downloaded",
            "Only visible retained history is available, deleted messages cannot be recovered",
            "Content availability depends on the bot's Discord access and intents",
            "Role labels come from the current guild cache, not historical role names",
            "Concatenate matching JSONL filenames in numbered archive order",
        ],
    }
    metadata_path = output_directory / "metadata.json"
    await _file_operation(_append_records, metadata_path, [metadata], cancel=cancel)
    archives = await _file_operation(
        _pack, output_directory, [metadata_path, *files], upload_limit, cancel=cancel
    )
    progress.phase = "uploading"
    return ChannelDump(
        archives, progress.counts["moderation"], progress.counts["members"], tuple(failures)
    )
