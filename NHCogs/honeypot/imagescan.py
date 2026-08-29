from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import typing
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import discord
from redbot.core import commands
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import box

from . import detection_runtime, diagnostics
from .detection_cases import ActionIntent, DetectionSignal
from .detection_runtime import DETECTION_IMAGE_READ_MAX_BYTES
from .diagnostics import REVIEW_DUMP_MAX_ZIP_BYTES
from .image_detector import ImageSample, image_hashes_from_bytes, match_image
from .settings import (
    BOOL_OPTIONS,
    IMAGE_SCAN_DETECTOR_ACTION_OPTIONS,
    GuildSettings,
)

_ = Translator("Honeypot", __file__)
log = logging.getLogger("red.Honeypot")

IMAGE_SCAN_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
IMAGE_SCAN_MAX_ATTACHMENTS = 4
IMAGE_ATTACHMENT_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = attachment.filename.lower()
    return any(filename.endswith(extension) for extension in IMAGE_ATTACHMENT_EXTENSIONS)


def plan_imagescan_event_cache_cleanup(
    files_root: Path,
    guild_id: int,
    *,
    delete: bool = False,
) -> dict[str, int]:
    guild_root = files_root / str(guild_id)
    plan = {
        "event_dirs": 0,
        "deleted_event_dirs": 0,
        "files": 0,
        "bytes": 0,
    }
    if not guild_root.exists():
        return plan
    for child in guild_root.iterdir():
        if child.name == "samples" or not child.is_dir():
            continue
        files = [path for path in child.rglob("*") if path.is_file()]
        plan["event_dirs"] += 1
        plan["files"] += len(files)
        plan["bytes"] += sum(path.stat().st_size for path in files)
        if delete:
            shutil.rmtree(child, ignore_errors=True)
            if not child.exists():
                plan["deleted_event_dirs"] += 1
    return plan


def match_imagescan_sample_identifier(
    rows: list[dict[str, typing.Any]],
    identifier: str,
) -> dict[str, typing.Any] | None:
    identifier = identifier.strip()
    if not identifier:
        return None
    exact = [
        row
        for row in rows
        if str(row.get("sample_id")) == identifier or str(row.get("sha256")) == identifier
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    prefix = [row for row in rows if str(row.get("sha256", "")).startswith(identifier)]
    return prefix[0] if len(prefix) == 1 else None


def is_imagescan_sample_path_safe(files_root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(files_root.resolve())
        return True
    except (OSError, ValueError):
        return False


def summarize_imagescan_sample_storage(rows: list[dict[str, typing.Any]]) -> dict[str, int]:
    stats = {
        "active_with_file": 0,
        "active_without_file": 0,
        "file_bytes": 0,
    }
    for row in rows:
        if not int(row.get("active", 0)):
            continue
        file_path = row.get("file_path")
        if file_path:
            stats["active_with_file"] += 1
            stats["file_bytes"] += int(row.get("file_size_bytes") or 0)
        else:
            stats["active_without_file"] += 1
    return stats


def _imagescan_is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = (attachment.filename or "").lower()
    return filename.endswith(IMAGE_SCAN_EXTENSIONS)


def _imagescan_safe_filename(filename: str | None, index: int) -> str:
    fallback = f"image-{index}.jpg"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or fallback).strip("._")
    return safe or fallback


async def _init_imagescan_store(cog) -> None:
    await asyncio.to_thread(cog._imagescan_store.initialize)


async def _imagescan_load_samples(cog, guild_id: int) -> list[ImageSample]:
    async with cog._imagescan_db_lock:
        return await asyncio.to_thread(cog._imagescan_store.load_active, guild_id)


async def _imagescan_model_state(cog, guild_id: int, configured_threshold: int) -> dict[str, typing.Any]:
    async with cog._imagescan_db_lock:
        return await asyncio.to_thread(
            cog._imagescan_store.verify,
            guild_id,
            configured_threshold,
        )


async def _imagescan_profile(cog, guild_id: int) -> dict[str, int]:
    async with cog._imagescan_db_lock:
        return await asyncio.to_thread(cog._imagescan_store.profile, guild_id)


async def _imagescan_increment_profile(cog, guild_id: int, increments: dict[str, int]) -> None:
    async with cog._imagescan_db_lock:
        await asyncio.to_thread(
            cog._imagescan_store.increment_profile,
            guild_id,
            increments,
        )


async def _imagescan_insert_sample(cog, sample: dict[str, typing.Any]) -> str:
    async with cog._imagescan_db_lock:
        return await asyncio.to_thread(cog._imagescan_store.insert, sample)


async def _imagescan_sample_rows(cog, guild_id: int, include_inactive: bool = False) -> list[dict[str, typing.Any]]:
    async with cog._imagescan_db_lock:
        return await asyncio.to_thread(
            cog._imagescan_store.rows,
            guild_id,
            include_inactive,
        )


async def _imagescan_update_sample_file(
    cog,
    guild_id: int,
    sample_id: str,
    file_path: str | None,
    file_size: int,
) -> None:
    async with cog._imagescan_db_lock:
        await asyncio.to_thread(
            cog._imagescan_store.update_file,
            guild_id,
            sample_id,
            file_path,
            file_size,
        )


async def _imagescan_delete_sample(cog, guild_id: int, sample_id: str) -> None:
    async with cog._imagescan_db_lock:
        await asyncio.to_thread(cog._imagescan_store.delete, guild_id, sample_id)


async def _imagescan_deactivate_sample(cog, guild_id: int, sample_id: str) -> None:
    async with cog._imagescan_db_lock:
        await asyncio.to_thread(
            cog._imagescan_store.deactivate,
            guild_id,
            sample_id,
        )


async def _imagescan_add_attachment_sample(
    cog,
    guild_id: int,
    message: discord.Message,
    attachment: discord.Attachment,
    index: int,
    decision: str,
    moderator_id: int | None,
) -> tuple[str, dict[str, typing.Any] | None]:
    try:
        data = await attachment.read(use_cached=True)
    except (discord.HTTPException, discord.Forbidden, discord.NotFound, TypeError) as exc:
        log.debug("Failed to read imagescan sample attachment %s: %r", attachment.filename, exc)
        return "error", None
    hashes = await asyncio.to_thread(image_hashes_from_bytes, data)
    sample_id = f"{message.id}-{index}-{hashes['sha256'][:12]}"
    sample_dir = cog._imagescan_files_path / str(guild_id) / "samples" / str(message.id)
    sample_dir.mkdir(parents=True, exist_ok=True)
    filename = _imagescan_safe_filename(attachment.filename, index)
    path = sample_dir / f"{index:03d}-{hashes['sha256'][:12]}-{filename}"
    try:
        path.write_bytes(data)
    except OSError as exc:
        log.debug("Failed to write imagescan sample %s: %r", path, exc)
        return "error", None
    sample = {
        "sample_id": sample_id,
        "guild_id": str(guild_id),
        "decision": decision,
        "sha256": hashes["sha256"],
        "phash": hashes["phash"],
        "dhash": hashes["dhash"],
        "ahash": hashes["ahash"],
        "source_message_id": str(message.id),
        "source_channel_id": str(message.channel.id),
        "source_jump_url": message.jump_url,
        "file_path": str(path),
        "file_size_bytes": len(data),
        "created_at": int(datetime.now(timezone.utc).timestamp()),
        "moderator_id": str(moderator_id) if moderator_id is not None else None,
    }
    status = await _imagescan_insert_sample(cog, sample)
    if status != "inserted":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return status, sample


async def _imagescan_add_file_sample(
    cog,
    guild_id: int,
    source_path: Path,
    decision: str,
    moderator_id: int | None,
) -> tuple[str, dict[str, typing.Any] | None]:
    try:
        data = await asyncio.to_thread(source_path.read_bytes)
    except OSError as exc:
        log.debug("Failed to read imagescan import file %s: %r", source_path, exc)
        return "error", None
    try:
        hashes = await asyncio.to_thread(image_hashes_from_bytes, data)
    except Exception:
        log.debug("Failed to hash imagescan import file %s", source_path, exc_info=True)
        return "error", None
    sample_id = f"import-{hashes['sha256'][:24]}"
    sample_dir = cog._imagescan_files_path / str(guild_id) / "samples" / "imports"
    sample_dir.mkdir(parents=True, exist_ok=True)
    filename = _imagescan_safe_filename(source_path.name, 1)
    path = sample_dir / f"{hashes['sha256'][:12]}-{filename}"
    sample = {
        "sample_id": sample_id,
        "guild_id": str(guild_id),
        "decision": decision,
        "sha256": hashes["sha256"],
        "phash": hashes["phash"],
        "dhash": hashes["dhash"],
        "ahash": hashes["ahash"],
        "source_message_id": None,
        "source_channel_id": None,
        "source_jump_url": source_path.as_posix(),
        "file_path": str(path),
        "file_size_bytes": len(data),
        "created_at": int(datetime.now(timezone.utc).timestamp()),
        "moderator_id": str(moderator_id) if moderator_id is not None else None,
    }
    try:
        async with cog._imagescan_db_lock:
            status = await asyncio.to_thread(
                cog._imagescan_store.publish_file_sample,
                sample,
                data,
                path,
            )
    except (OSError, sqlite3.Error) as exc:
        log.debug("Failed to write imagescan import sample %s: %r", path, exc)
        return "error", None
    return status, sample


async def _imagescan_add_bytes_sample(
    cog,
    guild_id: int,
    data: bytes,
    filename: str,
    source: str,
    decision: str,
    moderator_id: int | None,
) -> tuple[str, dict[str, typing.Any] | None]:
    try:
        hashes = await asyncio.to_thread(image_hashes_from_bytes, data)
    except Exception:
        log.debug("Failed to hash imagescan import item %s", filename, exc_info=True)
        return "error", None
    sample_id = f"upload-{hashes['sha256'][:24]}"
    sample_dir = cog._imagescan_files_path / str(guild_id) / "samples" / "uploads"
    sample_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = _imagescan_safe_filename(filename, 1)
    path = sample_dir / f"{hashes['sha256'][:12]}-{safe_filename}"
    try:
        await asyncio.to_thread(path.write_bytes, data)
    except OSError as exc:
        log.debug("Failed to write imagescan upload sample %s: %r", path, exc)
        return "error", None
    sample = {
        "sample_id": sample_id,
        "guild_id": str(guild_id),
        "decision": decision,
        "sha256": hashes["sha256"],
        "phash": hashes["phash"],
        "dhash": hashes["dhash"],
        "ahash": hashes["ahash"],
        "source_message_id": None,
        "source_channel_id": None,
        "source_jump_url": source,
        "file_path": str(path),
        "file_size_bytes": len(data),
        "created_at": int(datetime.now(timezone.utc).timestamp()),
        "moderator_id": str(moderator_id) if moderator_id is not None else None,
    }
    status = await _imagescan_insert_sample(cog, sample)
    if status != "inserted":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return status, sample


async def _imagescan_export_rows(cog, guild_id: int) -> list[dict[str, typing.Any]]:
    async with cog._imagescan_db_lock:
        return await asyncio.to_thread(cog._imagescan_store.export_rows, guild_id)


async def _imagescan_create_dump_archives(cog, guild_id: int) -> tuple[Path, list[Path]]:
    temp_root = Path(tempfile.mkdtemp(prefix="honeypot-imagescan-dump-"))
    data_root = temp_root / "data"
    zip_root = temp_root / "zips"
    files_root = data_root / "files"
    data_root.mkdir(parents=True, exist_ok=True)
    zip_root.mkdir(parents=True, exist_ok=True)
    rows = await _imagescan_export_rows(cog, guild_id)
    with (data_root / "imagescan.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    async with cog._imagescan_db_lock:
        samples = await asyncio.to_thread(
            cog._imagescan_store.export_samples, guild_id
        )
    if cog._imagescan_db_path.exists():
        shutil.copy2(cog._imagescan_db_path, data_root / "imagescan.sqlite")
    source_files_root = cog._imagescan_files_path / str(guild_id)
    if source_files_root.exists():
        for source in source_files_root.rglob("*"):
            if not source.is_file():
                continue
            target = files_root / str(guild_id) / source.relative_to(source_files_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    with (data_root / "samples.jsonl").open("w", encoding="utf-8") as handle:
        for sample in samples:
            created_at = int(sample["created_at"])
            source_path = Path(sample["file_path"]) if sample.get("file_path") else None
            archive_path = None
            if (
                source_path is not None
                and source_path.is_file()
                and is_imagescan_sample_path_safe(source_files_root, source_path)
            ):
                try:
                    relative_path = source_path.resolve().relative_to(
                        source_files_root.resolve()
                    )
                except ValueError:
                    relative_path = None
                if relative_path is not None:
                    archive_path = (Path("files") / str(guild_id) / relative_path).as_posix()
            exported = dict(sample)
            exported["created_at_iso"] = datetime.fromtimestamp(
                created_at, timezone.utc
            ).isoformat().replace("+00:00", "Z")
            exported["file"] = archive_path
            exported["active"] = bool(exported["active"])
            exported.pop("file_path", None)
            handle.write(json.dumps(exported, ensure_ascii=False) + "\n")
    archives = diagnostics._review_dump_zip_chunks(
        data_root, zip_root, REVIEW_DUMP_MAX_ZIP_BYTES
    )
    return temp_root, archives


async def _initial_image_signal(
    cog,
    message: discord.Message,
    guild_settings: GuildSettings,
    *,
    action_override: ActionIntent | None = None,
) -> DetectionSignal | None:
    if not guild_settings.imagescan_detector_enabled:
        return None
    image_count = sum(
        1 for attachment in message.attachments if is_image_attachment(attachment)
    )
    if not image_count:
        return None
    decision_started = perf_counter()
    profile = {
        "messages_scanned": 1,
        "messages_with_images": 1,
        "images_considered": min(image_count, IMAGE_SCAN_MAX_ATTACHMENTS),
        "images_ignored_over_limit": max(
            0, image_count - IMAGE_SCAN_MAX_ATTACHMENTS
        ),
    }
    samples = await cog._imagescan_load_samples(message.guild.id)
    if not any(sample.decision == "true_positive" for sample in samples):
        await cog._imagescan_increment_profile(message.guild.id, profile)
        return None
    state = await cog._imagescan_model_state(
        message.guild.id, guild_settings.imagescan_detector_threshold
    )
    if not state["valid"]:
        await cog._imagescan_increment_profile(message.guild.id, profile)
        return None
    matches: list[dict[str, object]] = []
    scans = await cog._scan_image_attachments(
        message,
        samples,
        int(state["effective_threshold"]),
        limit=IMAGE_SCAN_MAX_ATTACHMENTS,
        stop_after_match=True,
        batch_key=(message.guild.id, message.id),
    )
    successful_scans = [scan for scan in scans if scan["error"] is None]
    for stage in ("download", "hash", "compare"):
        profile[f"{stage}_ms_total"] = sum(
            int(scan[f"{stage}_ms"]) for scan in successful_scans
        )
        profile[f"{stage}_ms_count"] = len(successful_scans)
    profile["decision_ms_total"] = int(
        (perf_counter() - decision_started) * 1000
    )
    profile["decision_ms_count"] = 1
    for scan in scans:
        if scan["error"] is not None:
            log.debug(
                "Failed to scan initial imagescan attachment %s",
                scan["attachment"].filename,
            )
            continue
        result = scan["result"]
        if not result["matched"]:
            continue
        matches.append(
            {
                "position": scan["image_position"],
                "filename": scan["attachment"].filename,
                "hash_diff": result.get("score"),
                "threshold": result.get("threshold"),
                "exact_decision": result.get("exact_decision"),
            }
        )
        if result.get("exact_decision") == "true_positive":
            profile["exact_tp_hits"] = profile.get("exact_tp_hits", 0) + 1
        else:
            profile["flagged_tp_hits"] = profile.get("flagged_tp_hits", 0) + 1
    await cog._imagescan_increment_profile(message.guild.id, profile)
    if not matches:
        cog._initial_image_scan_batches.pop((message.guild.id, message.id), None)
        return None
    return DetectionSignal(
        detector="image",
        reason="Initial image scan matched known suspicious content",
        action=(
            action_override
            if action_override is not None
            else cog._signal_action(
                guild_settings.imagescan_detector_action.value,
                IMAGE_SCAN_DETECTOR_ACTION_OPTIONS,
            )
        ),
        decisive=True,
        metadata={"matches": tuple(matches)},
    )


async def _scan_image_attachments(
    cog,
    message: discord.Message,
    samples,
    threshold: int,
    *,
    capture_results: tuple[detection_runtime.CaptureResult, ...] = (),
    limit: int | None = None,
    stop_after_match: bool = False,
    batch_key: tuple[int, int] | None = None,
    skip_positions: frozenset[int] = frozenset(),
) -> tuple[dict[str, object], ...]:
    captures = {capture.position: capture for capture in capture_results}
    candidates = []
    image_position = 0
    for position, attachment in enumerate(message.attachments):
        if position in skip_positions:
            continue
        if not is_image_attachment(attachment):
            continue
        image_position += 1
        if limit is not None and image_position > limit:
            break
        candidates.append((position, image_position, attachment))

    async def scan_one(position, image_position, attachment):
        try:
            download_started = perf_counter()
            capture = captures.get(position)
            if capture is not None and capture.path is not None:
                data = await asyncio.wait_for(
                    asyncio.to_thread(
                        detection_runtime.read_file_bounded,
                        capture.path,
                        DETECTION_IMAGE_READ_MAX_BYTES,
                    ),
                    timeout=detection_runtime.DETECTION_ATTACHMENT_TIMEOUT_SECONDS,
                )
            elif capture is not None:
                raise RuntimeError(capture.error or "attachment capture is unavailable")
            else:
                data = await asyncio.wait_for(
                    detection_runtime.read_attachment_bounded(
                        attachment, DETECTION_IMAGE_READ_MAX_BYTES
                    ),
                    timeout=detection_runtime.DETECTION_ATTACHMENT_TIMEOUT_SECONDS,
                )
            download_ms = (perf_counter() - download_started) * 1000
            hash_started = perf_counter()
            hashes = await asyncio.to_thread(image_hashes_from_bytes, data)
            hash_ms = (perf_counter() - hash_started) * 1000
            compare_started = perf_counter()
            result = await asyncio.to_thread(match_image, hashes, samples, threshold)
            compare_ms = (perf_counter() - compare_started) * 1000
            error = None
        except Exception as exception:
            hashes = {}
            result = {}
            error = f"{type(exception).__name__}: {exception}"[:512]
            download_ms = 0.0
            hash_ms = 0.0
            compare_ms = 0.0
        return {
            "position": position,
            "image_position": image_position,
            "attachment": attachment,
            "hashes": hashes,
            "result": result,
            "error": error,
            "data": data if error is None else None,
            "download_ms": download_ms,
            "hash_ms": hash_ms,
            "compare_ms": compare_ms,
        }

    if stop_after_match:
        tasks = {
            position: asyncio.create_task(
                scan_one(position, image_position, attachment)
            )
            for position, image_position, attachment in candidates
        }
        cog._initial_image_scan_tasks.update(tasks.values())
        if batch_key is not None:
            cog._initial_image_scan_batches[batch_key] = tasks
        for task in tasks.values():
            task.add_done_callback(cog._initial_image_scan_tasks.discard)
        scans = []
        try:
            for completed in asyncio.as_completed(tasks.values()):
                scan = await completed
                scans.append(scan)
                if scan["error"] is None and scan["result"].get("matched"):
                    return tuple(scans)
        except BaseException:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            raise
        return tuple(scans)

    return tuple(
        [
            await scan_one(position, image_position, attachment)
            for position, image_position, attachment in candidates
        ]
    )


async def _scan_all_case_message_images(
    cog,
    message: discord.Message,
    guild_settings: GuildSettings,
    case_id: str,
    sequence: int,
    *,
    capture_results: tuple[detection_runtime.CaptureResult, ...],
) -> None:
    await cog._scan_case_message_images(
        message.guild.id,
        tuple(message.attachments),
        guild_settings,
        case_id,
        sequence,
        capture_results,
        initial_scan_key=(message.guild.id, message.id),
    )


async def _scan_case_message_images(
    cog,
    guild_id: int,
    attachments: tuple,
    guild_settings: GuildSettings,
    *,
    case_id: str,
    sequence: int,
    capture_results: tuple[detection_runtime.CaptureResult, ...],
    initial_scan_key: tuple[int, int] | None = None,
) -> None:
    if not attachments:
        return
    try:
        samples = await cog._imagescan_load_samples(guild_id)
        model = await cog._imagescan_model_state(
            guild_id,
            guild_settings.imagescan_detector_threshold,
        )
    except Exception as error:
        bounded = f"{type(error).__name__}: {error}"[:512]
        for position, attachment in enumerate(attachments):
            if is_image_attachment(attachment):
                await asyncio.to_thread(
                    cog._case_store.update_attachment_scan,
                    case_id, sequence, position, None, None,
                    match_metadata={}, error=bounded,
                )
        await cog._record_operational_failure(
            guild_id,
            "image_scan_setup",
            bounded,
            case_id=case_id,
            error=error,
        )
        return
    reused_scans = ()
    if initial_scan_key is not None:
        initial_tasks = cog._initial_image_scan_batches.pop(initial_scan_key, ())
        if initial_tasks:
            reused_scans = tuple(
                scan
                for scan in await asyncio.gather(*initial_tasks.values())
                if scan["error"] is None
            )
    reused_positions = frozenset(scan["position"] for scan in reused_scans)
    new_scans = await cog._scan_image_attachments(
        type("PersistedDetectionMessage", (), {"attachments": attachments})(),
        samples,
        model["effective_threshold"],
        capture_results=capture_results,
        skip_positions=reused_positions,
    )
    scans = tuple(sorted(reused_scans + new_scans, key=lambda item: item["position"]))
    unavailable_capture_positions = {
        capture.position
        for capture in capture_results
        if capture.status is not detection_runtime.CaptureStatus.CAPTURED
    }
    failed_scans = tuple(
        scan
        for scan in scans
        if scan["error"] is not None
        and scan["position"] not in unavailable_capture_positions
    )
    for scan in scans:
        position = scan["position"]
        if scan["error"] is None:
            hashes = scan["hashes"]
            await asyncio.to_thread(
                cog._case_store.update_attachment_scan,
                case_id,
                sequence,
                position,
                hashes.get("sha256"),
                hashes.get("phash"),
                match_metadata=scan["result"],
                error=None,
            )
        else:
            await asyncio.to_thread(
                cog._case_store.update_attachment_scan,
                case_id,
                sequence,
                position,
                None,
                None,
                match_metadata={},
                error=scan["error"],
            )
    if failed_scans:
        details = "; ".join(
            f"attachment {scan['position'] + 1}: {scan['error']}"
            for scan in failed_scans[:3]
        )
        await cog._record_operational_failure(
            guild_id,
            "image_scan",
            f"Failed to scan {len(failed_scans)} attachment(s): {details}"[:512],
            case_id=case_id,
        )


async def imagescan_add(cog, ctx: commands.Context) -> None:
    reference = getattr(ctx.message, "reference", None)
    if reference is None or reference.message_id is None:
        await ctx.send(_("Please reply to an offending message."))
        return
    target = reference.resolved
    if not isinstance(target, discord.Message):
        try:
            target = await ctx.channel.fetch_message(reference.message_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            await ctx.send(_("I couldn't fetch the replied message."))
            return
    attachments = [
        attachment
        for attachment in target.attachments
        if _imagescan_is_image_attachment(attachment)
    ][:IMAGE_SCAN_MAX_ATTACHMENTS]
    if not attachments:
        await ctx.send(_("No images found"))
        return
    inserted = duplicates = conflicts = errors = 0
    inserted_sample_ids: list[str] = []
    for index, attachment in enumerate(attachments, 1):
        status, sample = await _imagescan_add_attachment_sample(
            cog,
            ctx.guild.id,
            target,
            attachment,
            index,
            "true_positive",
            ctx.author.id,
        )
        if status == "inserted":
            inserted += 1
            if sample is not None:
                inserted_sample_ids.append(sample["sample_id"])
        elif status == "duplicate":
            duplicates += 1
        elif status == "conflict":
            conflicts += 1
        else:
            errors += 1
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    state = await cog._imagescan_model_state(
        ctx.guild.id,
        guild_settings.imagescan_detector_threshold,
    )
    if not state["valid"]:
        for sample_id in inserted_sample_ids:
            await _imagescan_deactivate_sample(cog, ctx.guild.id, sample_id)
        await cog._imagescan_model_state(
            ctx.guild.id,
            guild_settings.imagescan_detector_threshold,
        )
        await ctx.send(_("Rejected: TP/FP overlap.\nModel unchanged."))
        return
    parts = []
    if inserted:
        parts.append(_("{count} added").format(count=inserted))
    if duplicates:
        parts.append(_("{count} already known").format(count=duplicates))
    if conflicts:
        parts.append(_("{count} conflict").format(count=conflicts))
    if errors:
        parts.append(_("{count} failed").format(count=errors))
    await ctx.send(_("Imagescan add: {result}").format(result=", ".join(parts) or _("no changes")))


def _delete_imagescan_sample_file(path: Path) -> bool:
    """Delete a sample file when it is present and report whether it was removed."""
    if not path.exists():
        return False
    path.unlink()
    return True


async def imagescan_dropfile(cog, ctx: commands.Context, identifier: str) -> None:
    rows = await _imagescan_sample_rows(cog, ctx.guild.id)
    sample = match_imagescan_sample_identifier(rows, identifier)
    if sample is None:
        await ctx.send(_("No unique active image sample matched `{identifier}`.").format(identifier=identifier))
        return
    file_path = sample.get("file_path")
    deleted = False
    if file_path:
        path = Path(str(file_path))
        if not is_imagescan_sample_path_safe(cog._imagescan_files_path, path):
            await ctx.send(_("Refused to touch a file outside image scan storage."))
            return
        try:
            deleted = await asyncio.to_thread(_delete_imagescan_sample_file, path)
        except OSError:
            await ctx.send(_("Failed to delete sample file."))
            return
    await _imagescan_update_sample_file(cog, ctx.guild.id, str(sample["sample_id"]), None, 0)
    await ctx.send(
        _("Sample file dropped: `{sample_id}` (`{sha}`). File deleted: {deleted}. Hash retained.").format(
            sample_id=sample["sample_id"],
            sha=str(sample["sha256"])[:12],
            deleted=str(deleted).lower(),
        )
    )


async def imagescan_remove(cog, ctx: commands.Context, identifier: str) -> None:
    rows = await _imagescan_sample_rows(cog, ctx.guild.id)
    sample = match_imagescan_sample_identifier(rows, identifier)
    if sample is None:
        await ctx.send(_("No unique active image sample matched `{identifier}`.").format(identifier=identifier))
        return
    file_path = sample.get("file_path")
    deleted_file = False
    if file_path:
        path = Path(str(file_path))
        if not is_imagescan_sample_path_safe(cog._imagescan_files_path, path):
            await ctx.send(_("Refused to touch a file outside image scan storage."))
            return
        try:
            deleted_file = await asyncio.to_thread(_delete_imagescan_sample_file, path)
        except OSError:
            await ctx.send(_("Failed to delete sample file."))
            return
    await _imagescan_delete_sample(cog, ctx.guild.id, str(sample["sample_id"]))
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    state = await cog._imagescan_model_state(
        ctx.guild.id,
        guild_settings.imagescan_detector_threshold,
    )
    await ctx.send(
        _(
            "Sample removed: `{sample_id}` (`{sha}`). File deleted: {deleted}. "
            "Effective threshold: {threshold}"
        ).format(
            sample_id=sample["sample_id"],
            sha=str(sample["sha256"])[:12],
            deleted=str(deleted_file).lower(),
            threshold=state["effective_threshold"],
        )
    )


async def imagescan_detector_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).imagescan_detector_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
        return
    await cog.config.guild(ctx.guild).imagescan_detector_enabled.set(value)
    await ctx.send(_("Image detector enabled set to {value}").format(value=value))


async def imagescan_detector_action(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).imagescan_detector_action()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v,
                options=cog._format_options(IMAGE_SCAN_DETECTOR_ACTION_OPTIONS),
            )
        )
        return
    if value not in IMAGE_SCAN_DETECTOR_ACTION_OPTIONS:
        await ctx.send(
            _("Choose one of: {options}").format(
                options=cog._format_options(IMAGE_SCAN_DETECTOR_ACTION_OPTIONS),
            )
        )
        return
    await cog.config.guild(ctx.guild).imagescan_detector_action.set(value)
    await ctx.send(_("Image detector action set to {value}").format(value=value))


async def imagescan_detector_threshold(cog, ctx: commands.Context, value: int = None) -> None:
    if value is None:
        raw_config = await cog.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        state = await cog._imagescan_model_state(
            ctx.guild.id,
            guild_settings.imagescan_detector_threshold,
        )
        await ctx.send(
            _("Threshold: {configured} effective {effective}").format(
                configured=state["configured_threshold"],
                effective=state["effective_threshold"],
            )
        )
        return
    if value < 0 or value > 100:
        await ctx.send(_("Threshold must be between 0 and 100."))
        return
    await cog.config.guild(ctx.guild).imagescan_detector_threshold.set(value)
    await cog._imagescan_model_state(ctx.guild.id, value)
    await ctx.send(_("Image detector threshold set to {value}").format(value=value))


async def imagescan_model_rebuild(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    state = await cog._imagescan_model_state(
        ctx.guild.id,
        guild_settings.imagescan_detector_threshold,
    )
    if not state["valid"]:
        await ctx.send(_("Rejected: TP/FP overlap.\nModel unchanged."))
        return
    await ctx.send(
        _("Model rebuilt. Effective threshold: {threshold}").format(
            threshold=state["effective_threshold"],
        )
    )


async def imagescan_status(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    state = await cog._imagescan_model_state(
        ctx.guild.id,
        guild_settings.imagescan_detector_threshold,
    )
    profile = await cog._imagescan_profile(ctx.guild.id)
    total_samples = int(state["sample_count_tp"]) + int(state["sample_count_fp"])
    sample_rows = await _imagescan_sample_rows(cog, ctx.guild.id)
    storage = summarize_imagescan_sample_storage(sample_rows)

    def avg(total_key: str, count_key: str) -> int:
        count = profile.get(count_key, 0)
        return int(profile.get(total_key, 0) / count) if count else 0

    lines = [
        f"Enabled: {cog._format_bool_setting(guild_settings.imagescan_detector_enabled)}",
        f"Action: {guild_settings.imagescan_detector_action.value}",
        f"Threshold: {state['configured_threshold']} effective {state['effective_threshold']}",
        f"Samples: {state['sample_count_tp']} TP, {state['sample_count_fp']} FP, {total_samples} total",
        (
            "Sample files: "
            f"{storage['active_with_file']} stored, "
            f"{storage['active_without_file']} hash-only"
        ),
        f"Sample storage: {cog._format_bytes(storage['file_bytes'])}",
        (
            "Scanned: "
            f"{profile.get('messages_scanned', 0)} messages, "
            f"{profile.get('images_considered', 0)} images considered, "
            f"{profile.get('images_ignored_over_limit', 0)} images ignored over limit"
        ),
        f"Hits: {profile.get('exact_tp_hits', 0)} exact TP, {profile.get('flagged_tp_hits', 0)} flagged TP",
        f"Decision latency: avg {avg('decision_ms_total', 'decision_ms_count')} ms",
        (
            "Download/hash/compare: avg "
            f"{avg('download_ms_total', 'download_ms_count')} / "
            f"{avg('hash_ms_total', 'hash_ms_count')} / "
            f"{avg('compare_ms_total', 'compare_ms_count')} ms"
        ),
    ]
    await ctx.send(_("**Image scan status:**\n") + box("\n".join(lines)))


async def imagescan_dump(cog, ctx: commands.Context) -> None:
    temp_root: Path | None = None
    try:
        temp_root, archives = await _imagescan_create_dump_archives(cog, ctx.guild.id)
        if not archives:
            await ctx.send(_("No image scan dump files were created"))
            return
        await ctx.send(_("Image scan dump created. Sending {count} file(s).").format(count=len(archives)))
        for archive in archives:
            await ctx.send(file=discord.File(archive))
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


async def imagescan_import_tp_zip(cog, ctx: commands.Context) -> None:
    attachments = list(ctx.message.attachments)
    reference = getattr(ctx.message, "reference", None)
    if not attachments and reference is not None and reference.message_id is not None:
        target = reference.resolved
        if not isinstance(target, discord.Message):
            try:
                target = await ctx.channel.fetch_message(reference.message_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                target = None
        if isinstance(target, discord.Message):
            attachments = list(target.attachments)
    zip_attachments = [
        attachment
        for attachment in attachments
        if (attachment.filename or "").lower().endswith(".zip")
    ]
    if not zip_attachments:
        await ctx.send(_("Attach a .zip file or reply to a message with a .zip file."))
        return
    progress = await ctx.send(_("Importing zip file(s)..."))
    inserted = duplicates = conflicts = errors = skipped = 0
    error_notes: list[str] = []
    inserted_sample_ids: list[str] = []
    processed = 0
    for attachment in zip_attachments:
        attachment_name = attachment.filename or "attachment.zip"
        try:
            archive_data = await attachment.read()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound, TypeError) as primary_exc:
            try:
                archive_data = await attachment.read(use_cached=True)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound, TypeError) as cached_exc:
                errors += 1
                error_notes.append(
                    _("{filename}: download failed ({error})").format(
                        filename=attachment_name,
                        error=type(cached_exc if cached_exc is not None else primary_exc).__name__,
                    )
                )
                continue
        try:
            archive = zipfile.ZipFile(io.BytesIO(archive_data))
        except zipfile.BadZipFile:
            errors += 1
            error_notes.append(
                _("{filename}: invalid zip file").format(filename=attachment_name)
            )
            continue
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                filename = Path(info.filename).name
                if Path(filename).suffix.lower() not in IMAGE_ATTACHMENT_EXTENSIONS:
                    skipped += 1
                    continue
                try:
                    data = archive.read(info)
                except (RuntimeError, OSError, zipfile.BadZipFile):
                    errors += 1
                    continue
                status, sample = await _imagescan_add_bytes_sample(
                    cog,
                    ctx.guild.id,
                    data,
                    filename,
                    f"{attachment.url}#{info.filename}",
                    "true_positive",
                    ctx.author.id,
                )
                if status == "inserted":
                    inserted += 1
                    if sample is not None:
                        inserted_sample_ids.append(sample["sample_id"])
                elif status == "duplicate":
                    duplicates += 1
                elif status == "conflict":
                    conflicts += 1
                else:
                    errors += 1
                processed += 1
                if processed == 1 or processed % 25 == 0:
                    try:
                        await progress.edit(
                            content=_("Imported {count} image(s)...").format(count=processed)
                        )
                    except discord.HTTPException:
                        pass
                    await asyncio.sleep(0)
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    state = await cog._imagescan_model_state(
        ctx.guild.id,
        guild_settings.imagescan_detector_threshold,
    )
    if not state["valid"]:
        for sample_id in inserted_sample_ids:
            await _imagescan_deactivate_sample(cog, ctx.guild.id, sample_id)
        await cog._imagescan_model_state(
            ctx.guild.id,
            guild_settings.imagescan_detector_threshold,
        )
        await progress.edit(content=_("Rejected: TP/FP overlap.\nModel unchanged."))
        return
    final_message = _(
        "Import finished: {inserted} added, {duplicates} already known, "
        "{conflicts} conflicts, {errors} failed, {skipped} skipped. "
        "Effective threshold: {threshold}"
    ).format(
        inserted=inserted,
        duplicates=duplicates,
        conflicts=conflicts,
        errors=errors,
        skipped=skipped,
        threshold=state["effective_threshold"],
    )
    if error_notes:
        shown_errors = "\n".join(f"- {note}" for note in error_notes[:5])
        if len(error_notes) > 5:
            shown_errors += _("\n- and {count} more").format(count=len(error_notes) - 5)
        final_message = f"{final_message}\n{_('Failed:')}\n{shown_errors}"
    await progress.edit(content=final_message)


async def config_imagescan(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    state = await cog._imagescan_model_state(
        ctx.guild.id,
        guild_settings.imagescan_detector_threshold,
    )
    await cog._send_config_dump(
        ctx,
        _("Image scan config"),
        [
            (
                _("Enabled"),
                cog._format_bool_setting(
                    guild_settings.imagescan_detector_enabled
                ),
            ),
            (_("Action"), guild_settings.imagescan_detector_action.value),
            (_("Threshold"), f"{state['configured_threshold']} effective {state['effective_threshold']}"),
        ],
    )
