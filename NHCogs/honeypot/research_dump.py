"""Dump channel history for offline research without interpreting log messages."""

import argparse
import asyncio
import hashlib
import json
import math
import mimetypes
import re
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

import aiohttp

_WRITE_BATCH_SIZE = 250
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504, 524}
_RETRY_BASE_SECONDS = 5
_RETRY_MAX_SECONDS = 60
_RETRY_MAX_EXPONENT = 4
_FILE_CHUNK_SIZE = 64 * 1024
_ATTACHMENT_DOWNLOAD_ATTEMPTS = 3
_HAS_TYPES = ("attachments", "images", "videos", "embeds", "links")


def _boundary(value: str) -> datetime | int:
    if value.isdigit():
        return int(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use an ISO date/time or a message ID") from error
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _has_content(message, kind: str | None) -> bool:
    if kind is None:
        return True
    if kind == "attachments":
        return bool(message.attachments)
    if kind == "embeds":
        return bool(message.embeds)
    if kind == "links":
        return bool(re.search(r"https?://", message.content)) or any(
            embed.to_dict().get("url") for embed in message.embeds
        )
    media_type = "image" if kind == "images" else "video"
    return any(
        (attachment.content_type or mimetypes.guess_type(attachment.filename)[0] or "").startswith(media_type + "/")
        for attachment in message.attachments
    ) or any(
        embed.to_dict().get(media_type) or embed.to_dict().get("type") == media_type
        for embed in message.embeds
    )


@dataclass(frozen=True)
class DumpOptions:
    """One export selection, shared by parsing, history cursors and metadata."""

    limit: int | None = None
    after: datetime | int | None = None
    before: datetime | int | None = None
    order: str | None = None
    author_id: int | None = None
    has: str | None = None
    download_attachments: bool = False

    def __post_init__(self):
        if self.limit is not None and self.limit < 1:
            raise ValueError("Limit must be a positive number of matching messages per channel")
        if self.author_id is not None and self.author_id < 1:
            raise ValueError("Author must be a positive user ID")
        if self.order not in (None, "oldest", "newest") or self.has not in (None, *_HAS_TYPES):
            raise ValueError("Invalid order or content filter")
        if self.after is not None and self.before is not None and type(self.after) is type(self.before) and self.after >= self.before:
            raise ValueError("After must be earlier than before")

    @property
    def oldest_first(self) -> bool:
        return self.order == "oldest" or (self.order is None and self.limit is None)

    def history_arguments(self, cutoff: datetime, last_message=None) -> dict:
        before = min(self.before, cutoff) if isinstance(self.before, datetime) else self.before
        arguments: dict[str, object] = {
            "limit": None, "oldest_first": self.oldest_first,
            "before": before if before is not None else cutoff,
        }
        if self.after is not None:
            arguments["after"] = self.after
        for key in ("before", "after"):
            if isinstance(arguments.get(key), int):
                arguments[key] = SimpleNamespace(id=arguments[key])
        if last_message is not None:
            arguments["after" if self.oldest_first else "before"] = last_message
        return arguments

    def matches(self, message, cutoff: datetime) -> bool:
        if message.created_at >= cutoff:
            return False
        for boundary, after in ((self.after, True), (self.before, False)):
            if boundary is None:
                continue
            value = message.id if isinstance(boundary, int) else message.created_at
            if (after and value <= boundary) or (not after and value >= boundary):
                return False
        return (self.author_id is None or message.author.id == self.author_id) and _has_content(message, self.has)

    def metadata(self) -> dict:
        return {
            "limit_per_channel": self.limit,
            "after": self.after.isoformat() if isinstance(self.after, datetime) else str(self.after) if self.after is not None else None,
            "before": self.before.isoformat() if isinstance(self.before, datetime) else str(self.before) if self.before is not None else None,
            "order": "oldest" if self.oldest_first else "newest",
            "author_id": str(self.author_id) if self.author_id is not None else None,
            "has": self.has,
            "download_attachments": self.download_attachments,
        }


class _DumpParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_arguments(arguments: str) -> tuple[list[int], DumpOptions]:
    parser = _DumpParser(add_help=False, allow_abbrev=False)
    parser.add_argument("channel_ids", nargs="+", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--after", type=_boundary)
    parser.add_argument("--before", type=_boundary)
    parser.add_argument("--order", choices=("newest", "oldest"))
    parser.add_argument("--author", type=int, dest="author_id")
    parser.add_argument("--has", choices=_HAS_TYPES)
    parser.add_argument("--attachments", action="store_true", dest="download_attachments")
    values = vars(parser.parse_intermixed_args(shlex.split(arguments)))
    channel_ids = list(dict.fromkeys(values.pop("channel_ids")))
    return channel_ids, DumpOptions(**values)


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
            error,
            (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
                TimeoutError,
            ),
        )
    ):
        return None
    if status is not None and status not in _RETRYABLE_HTTP_STATUSES:
        return None
    if retry_after is None:
        response = getattr(error, "response", None)
        headers = getattr(error, "headers", None)
        if headers is None and response is not None:
            headers = response.headers
        if headers is not None:
            retry_after = headers.get("Retry-After")
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

    phase: str = "starting"
    counts: dict[int, int] = field(default_factory=dict)
    channel_name: str | None = None
    latest_message_at: datetime | None = None
    retry_until: float | None = None
    attachments_downloaded: int = 0
    attachments_failed: int = 0
    attachment_bytes: int = 0


@dataclass(frozen=True)
class ChannelDump:
    archives: tuple[Path, ...]
    message_counts: dict[int, int]
    failures: tuple[tuple[int, Exception], ...] = ()
    attachment_failures: int = 0

    @property
    def complete(self) -> bool:
        return not self.failures and not self.attachment_failures


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


def _archive_budget(upload_limit: int) -> int:
    budget = int(upload_limit * 0.98) - 2048
    if budget <= 0:
        raise ValueError("Upload limit is too small for the research dump")
    return budget


def _append_bytes(path: Path, data: bytes, cancel: Event) -> None:
    _check_cancel(cancel)
    with path.open("ab") as stream:
        stream.write(data)


def _remove_files(paths: list[Path], cancel: Event) -> None:
    for path in paths:
        _check_cancel(cancel)
        path.unlink(missing_ok=True)


class _AttachmentFiles:
    """Stream selected files to bounded ZIP entries and retain their provenance."""

    def __init__(self, directory, upload_limit, session, progress, cancel):
        self.directory = directory / "attachments"
        self.directory.mkdir()
        self.budget = _archive_budget(upload_limit)
        self.session, self.progress, self.cancel = session, progress, cancel
        self.files: list[Path] = []

    async def capture(self, message, record):
        self.progress.phase = "attachments"
        try:
            for index, (attachment, item) in enumerate(zip(message.attachments, record["attachments"], strict=True), 1):
                leaf = attachment.filename.replace("\\", "/").rsplit("/", 1)[-1]
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", leaf)[:150] or "attachment"
                item.update(await self._download(attachment, f"{message.id}_{index:02d}_{safe}"))
        finally:
            self.progress.phase = "reading"

    async def _download(self, attachment, stem):
        for attempt in range(_ATTACHMENT_DOWNLOAD_ATTEMPTS):
            paths = []
            try:
                checksum = await self._stream(attachment, stem, paths)
            except Exception as error:
                await _file_operation(_remove_files, paths, cancel=self.cancel)
                delay = _retry_delay(error, attempt)
                if delay is not None and attempt + 1 < _ATTACHMENT_DOWNLOAD_ATTEMPTS:
                    self.progress.retry_until = asyncio.get_running_loop().time() + delay
                    try:
                        await asyncio.sleep(delay)
                    finally:
                        self.progress.retry_until = None
                    continue
                self.progress.attachments_failed += 1
                return {"download_status": "failed", "local_files": [],
                        "download_error": {"type": type(error).__name__, "status": getattr(error, "status", None)}}
            self.files.extend(paths)
            self.progress.attachments_downloaded += 1
            return {"download_status": "downloaded", "sha256": checksum,
                    "local_files": [path.relative_to(self.directory.parent).as_posix() for path in paths]}
        raise AssertionError("unreachable")

    async def _stream(self, attachment, stem, paths):
        url = urlsplit(attachment.url)
        if url.scheme != "https" or url.hostname not in ("cdn.discordapp.com", "media.discordapp.net"):
            raise ValueError("Attachment URL is not on Discord's CDN")
        checksum = hashlib.sha256()
        size = part_size = 0
        split = attachment.size > self.budget
        async with self.session.get(attachment.url, allow_redirects=False) as response:
            response.raise_for_status()
            async for received in response.content.iter_chunked(_FILE_CHUNK_SIZE):
                data = received
                size += len(data)
                if size > attachment.size:
                    raise ValueError("Attachment is larger than its Discord metadata")
                checksum.update(data)
                self.progress.attachment_bytes += len(data)
                while data:
                    if not paths or part_size == self.budget:
                        suffix = f".part{len(paths) + 1:03d}" if split else ""
                        paths.append(self.directory / (stem + suffix))
                        part_size = 0
                    chunk, data = data[:self.budget - part_size], data[self.budget - part_size:]
                    await _file_operation(_append_bytes, paths[-1], chunk, cancel=self.cancel)
                    part_size += len(chunk)
        if size != attachment.size:
            raise ValueError("Attachment download is incomplete")
        if not paths:
            paths.append(self.directory / stem)
            await _file_operation(_append_bytes, paths[-1], b"", cancel=self.cancel)
        return checksum.hexdigest()


async def _collect_channel(
    channel, path: Path, progress: DumpProgress, *, cutoff: datetime, cancel: Event,
    options: DumpOptions, downloads: _AttachmentFiles | None = None,
) -> Exception | None:
    progress.phase = "reading"
    progress.channel_name = channel.name
    progress.counts.setdefault(channel.id, 0)
    progress.latest_message_at = None
    pending = []
    last_message = None
    attempts = 0
    while True:
        try:
            async for message in channel.history(**options.history_arguments(cutoff, last_message)):
                if last_message is not None and (
                    (options.oldest_first and message.id <= last_message.id)
                    or (not options.oldest_first and message.id >= last_message.id)
                ):
                    continue
                last_message = message
                progress.latest_message_at = message.created_at
                attempts = 0
                if not options.matches(message, cutoff):
                    continue
                record = _message_record(message, channel.id)
                if downloads is not None:
                    await downloads.capture(message, record)
                pending.append(record)
                progress.counts[channel.id] += 1
                if len(pending) >= _WRITE_BATCH_SIZE:
                    await _file_operation(_append_records, path, pending, cancel=cancel)
                    pending = []
                if options.limit is not None and progress.counts[channel.id] >= options.limit:
                    break
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
                    archive, path.relative_to(directory).as_posix(), iter(lambda: stream.read(_FILE_CHUNK_SIZE), b""), cancel
                )
    _check_cancel(cancel)
    if archive_path.stat().st_size <= upload_limit:
        return (archive_path,)
    archive_path.unlink()
    budget = _archive_budget(upload_limit)
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
        if path.parent != directory:
            _check_cancel(cancel)
            if path.stat().st_size > budget:
                raise ValueError("Attachment part exceeds the upload limit")
            if entries and size + path.stat().st_size > budget:
                flush()
            entries.append((path.relative_to(directory).as_posix(), path.read_bytes()))
            size += path.stat().st_size
            continue
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
    channels,
    output_directory: Path,
    *,
    bot_id: int,
    upload_limit: int,
    cutoff: datetime,
    progress: DumpProgress | None = None,
    options: DumpOptions | None = None,
) -> ChannelDump:
    """Read each source once. The caller owns private delivery and temp cleanup."""
    if not channels:
        raise ValueError("Choose at least one source channel")
    progress = progress if progress is not None else DumpProgress()
    options = options if options is not None else DumpOptions()
    cancel = Event()
    role_labels = {str(role.id): role.name for role in channels[0].guild.roles}
    role_labels_at = datetime.now(timezone.utc).isoformat()
    files = []
    failures = []
    downloads = None
    async with AsyncExitStack() as stack:
        if options.download_attachments:
            session = await stack.enter_async_context(aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600, connect=15, sock_read=30),
            ))
            downloads = _AttachmentFiles(output_directory, upload_limit, session, progress, cancel)
        for channel in channels:
            path = output_directory / f"channel-{channel.id}.jsonl"
            failure = await _collect_channel(channel, path, progress, cutoff=cutoff, cancel=cancel, options=options, downloads=downloads)
            if failure is not None:
                failures.append((channel.id, failure))
            files.append(path)
        if downloads is not None:
            files.extend(downloads.files)
    progress.phase = "packaging"
    metadata = {
        "format_version": 2,
        "export_type": "channel_history_dump",
        "complete": not failures and not progress.attachments_failed,
        "attachment_errors": progress.attachments_failed,
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
        "selection": options.metadata(),
        "channels": [
            {
                "id": str(channel.id),
                "name": channel.name,
                "file": f"channel-{channel.id}.jsonl",
                "messages": progress.counts[channel.id],
            }
            for channel in channels
        ],
        "current_role_labels": role_labels,
        "role_labels_observed_at": role_labels_at,
        "limitations": [
            "No ban, account or role interpretation is performed",
            "Message authors are observed at export time, embedded log identities remain unchanged",
            ("Attachments are downloaded to local_files. Concatenate multiple parts in listed order and verify sha256"
             if options.download_attachments else "Attachments are metadata and URLs only, files and avatars are not downloaded"),
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
    return ChannelDump(archives, dict(progress.counts), tuple(failures), progress.attachments_failed)
