"""Channel-scoped Discord GIF embed interception for Honeypot."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse

import discord
from redbot.core import commands, modlog
from redbot.core.i18n import Translator

from . import settings
from .settings import GuildSettings

log = logging.getLogger("red.Honeypot")

SEEN_MESSAGE_LIMIT = 4096
MAX_SECONDARY_MESSAGE_LENGTH = 1900
GIF_RETENTION_MIN_SECONDS = 0
GIF_RETENTION_MAX_SECONDS = 60
GIF_THRESHOLD_MIN = 2
GIF_THRESHOLD_MAX = 20
GIF_WINDOW_MIN_SECONDS = 5
GIF_WINDOW_MAX_SECONDS = 3600
GIF_MUTE_MIN_SECONDS = 60
GIF_MUTE_MAX_SECONDS = 604800
_URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,!?:;'\")]}<>"
_GIF_PROVIDER_NAMES = frozenset(("giphy", "tenor"))
_GIF_PROVIDER_HOSTS = frozenset(
    (
        "giphy.com",
        "i.giphy.com",
        "media.giphy.com",
        "media0.giphy.com",
        "media1.giphy.com",
        "media2.giphy.com",
        "media3.giphy.com",
        "media4.giphy.com",
        "tenor.com",
        "media.tenor.com",
        "www.giphy.com",
        "www.tenor.com",
    )
)
_ = Translator("Honeypot", __file__)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _collection(value: Any) -> Iterable[Any]:
    if value is None or isinstance(value, (str, bytes, bytearray, Mapping)):
        return ()
    try:
        iter(value)
    except TypeError:
        return ()
    return value


def _urls_in_text(value: Any) -> Iterable[str]:
    if not isinstance(value, str):
        return ()
    return (
        match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        for match in _URL_PATTERN.finditer(value)
    )


def _is_gif_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return urlparse(value).path.casefold().endswith(".gif")
    except ValueError:
        return False


def _hostname(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return urlparse(value).hostname
    except ValueError:
        return None


def _embed_urls(embed: Any) -> Iterable[str]:
    for field_name in ("url", "source", "image", "thumbnail", "video"):
        field_value = _field(embed, field_name)
        url = field_value if isinstance(field_value, str) else _field(field_value, "url")
        if isinstance(url, str):
            yield url


_REMOTE_ANIMATED_IMAGE_TYPES = frozenset(
    ("image/apng", "image/avif", "image/avif-sequence", "image/png", "image/webp")
)
_REMOTE_ANIMATED_IMAGE_SUFFIXES = (".apng", ".avif", ".avifs", ".png", ".webp")


def _is_remote_animated_image_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return urlparse(value).path.casefold().endswith(
            _REMOTE_ANIMATED_IMAGE_SUFFIXES
        )
    except ValueError:
        return False


_DISCORD_MEDIA_HOSTS = frozenset(
    {
        "cdn.discordapp.com",
        "media.discordapp.net",
    }
)
_DISCORD_SIGNATURE_QUERY_FIELDS = frozenset({"ex", "is", "hm"})


def _stable_query(value: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (name, field_value)
            for name, field_value in parse_qsl(value, keep_blank_values=True)
            if name not in _DISCORD_SIGNATURE_QUERY_FIELDS
        )
    )


def _same_remote_media_candidate(original: str, current: str) -> bool:
    if original == current:
        return True
    try:
        original_url = urlparse(original)
        current_url = urlparse(current)
        original_host = (original_url.hostname or "").casefold()
        current_host = (current_url.hostname or "").casefold()
        return (
            original_url.scheme.casefold() == "https"
            and current_url.scheme.casefold() == "https"
            and original_host in _DISCORD_MEDIA_HOSTS
            and current_host == original_host
            and original_url.username is None
            and current_url.username is None
            and original_url.password is None
            and current_url.password is None
            and original_url.port in (None, 443)
            and current_url.port in (None, 443)
            and original_url.path == current_url.path
            and original_url.params == current_url.params
            and _stable_query(original_url.query) == _stable_query(current_url.query)
        )
    except ValueError:
        return False


def _prioritize_remote_media_candidates(candidates: Iterable[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(candidates))

    def priority(url: str) -> int:
        try:
            parsed = urlparse(url)
            query_names = {name for name, _value in parse_qsl(parsed.query)}
            if (
                (parsed.hostname or "").casefold() in _DISCORD_MEDIA_HOSTS
                and query_names >= _DISCORD_SIGNATURE_QUERY_FIELDS
            ):
                return 0
        except ValueError:
            pass
        return 1

    return tuple(sorted(unique, key=priority))


def _remote_media_candidates_from_fields(
    *,
    embeds: Any = (),
    attachments: Any = (),
    content: Any = "",
) -> Iterable[str]:
    seen: set[str] = set()
    for attachment in _collection(attachments):
        media_type = str(_field(attachment, "content_type", "")).casefold()
        filename = str(_field(attachment, "filename", "")).casefold()
        if (
            media_type not in _REMOTE_ANIMATED_IMAGE_TYPES
            and not filename.endswith(_REMOTE_ANIMATED_IMAGE_SUFFIXES)
        ):
            continue
        for field_name in ("url", "proxy_url"):
            url = _field(attachment, field_name)
            if isinstance(url, str) and url not in seen:
                seen.add(url)
                yield url
    for embed in _collection(embeds):
        for field_name in ("url", "image", "thumbnail", "video"):
            field_value = _field(embed, field_name)
            candidates = (
                (field_value,)
                if isinstance(field_value, str)
                else (_field(field_value, "url"), _field(field_value, "proxy_url"))
            )
            for url in candidates:
                if _is_remote_animated_image_url(url) and url not in seen:
                    seen.add(url)
                    yield url
    for url in _urls_in_text(content):
        if _is_remote_animated_image_url(url) and url not in seen:
            seen.add(url)
            yield url


def _remote_media_candidates(message: Any) -> Iterable[str]:
    return _remote_media_candidates_from_fields(
        embeds=getattr(message, "embeds", ()),
        attachments=getattr(message, "attachments", ()),
        content=getattr(message, "content", ""),
    )


def _has_gif_provider_provenance(embed: Any, urls: Iterable[str]) -> bool:
    if str(_field(embed, "type", "")).casefold() != "video":
        return False
    provider = _field(embed, "provider")
    provider_name = str(_field(provider, "name", "")).strip().casefold()
    if provider_name in _GIF_PROVIDER_NAMES:
        return True
    provider_url = _field(provider, "url")
    candidate_urls = (*urls, provider_url)
    return any(_hostname(url) in _GIF_PROVIDER_HOSTS for url in candidate_urls)


def _attachment_has_gif_evidence(attachment: Any) -> bool:
    media_type = str(_field(attachment, "content_type", "")).casefold()
    filename = str(_field(attachment, "filename", "")).casefold()
    return (
        media_type == "image/gif"
        or filename.endswith(".gif")
        or any(_is_gif_url(url) for url in _urls_in_text(_field(attachment, "url")))
    )


def _embed_has_gif_evidence(embed: Any) -> bool:
    if str(_field(embed, "type", "")).casefold() == "gifv":
        return True
    urls = tuple(_embed_urls(embed))
    return any(_is_gif_url(url) for url in urls) or _has_gif_provider_provenance(
        embed, urls
    )


def has_gif_evidence(
    *,
    embeds: Iterable[Any] = (),
    attachments: Iterable[Any] = (),
    content: Any = "",
) -> bool:
    """Return whether message fields contain explicit GIF-media evidence."""

    return gif_evidence_source(
        embeds=embeds,
        attachments=attachments,
        content=content,
    ) is not None


def gif_evidence_source(
    *,
    embeds: Any = (),
    attachments: Any = (),
    content: Any = "",
) -> str | None:
    if any(
        _attachment_has_gif_evidence(item) for item in _collection(attachments)
    ):
        return "attachment"
    if any(_is_gif_url(url) for url in _urls_in_text(content)):
        return "content"
    if any(_embed_has_gif_evidence(embed) for embed in _collection(embeds)):
        return "embed"
    return None


def channel_scope_id(channel: Any) -> int:
    """Return the configured parent-channel scope for a channel or thread."""

    return getattr(channel, "parent_id", None) or channel.id


def render_icbm_frame(author_mention: str, *, rocket_position: int) -> str:
    """Render one frame on the fixed ten-dash horizontal track."""

    travelled = "─" * rocket_position
    remaining = "─" * (10 - rocket_position)
    return f"{travelled}🚀{remaining}🎯 {author_mention}'s GIF"


async def _record_http_failure(cog: Any, message: Any, action: str, error: Exception) -> None:
    await cog._record_operational_failure(
        message.guild.id,
        "gif_detector",
        f"{action}: {type(error).__name__}: {error}",
    )


async def _delete_message(cog: Any, message: Any, action: str) -> str:
    try:
        await message.delete()
    except discord.NotFound:
        return "already_missing"
    except discord.HTTPException as error:
        await _record_http_failure(cog, message, action, error)
        return "failed"
    return "deleted"


@dataclass(frozen=True)
class _ShotDiagnostic:
    cog: Any
    record: Any
    admitted: str
    guild_id: int

    async def finish(
        self,
        *,
        state: str,
        source_delete: str,
        response_cleanup: str,
    ) -> None:
        content = (
            f"{self.admitted}\n"
            f"state={state} source_delete={source_delete} "
            f"response_cleanup={response_cleanup}"
        )
        try:
            await self.record.edit(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            log.warning("Could not update GIF diagnostic record", exc_info=True)
            try:
                await self.cog._record_operational_failure(
                    self.guild_id,
                    "gif_debug",
                    f"Could not update GIF diagnostic record: {type(error).__name__}: {error}",
                )
            except Exception:
                log.warning("Could not record GIF diagnostic update failure", exc_info=True)


async def _start_diagnostic(
    cog: Any,
    message: Any,
    configured: GuildSettings,
    *,
    event_source: str,
    evidence_source: str,
    path: str,
) -> _ShotDiagnostic | None:
    if not configured.gif_detector_debug_enabled:
        return None
    channel_id = configured.gif_detector_debug_channel
    if channel_id is None:
        return None
    channel = cog._get_text_channel_or_thread(message.guild, channel_id)
    if channel is None:
        return None
    guild = message.guild
    author = message.author
    get_member = getattr(guild, "get_member", None)
    author_cached = bool(callable(get_member) and get_member(author.id) is not None)
    jump_url = getattr(message, "jump_url", None) or "unavailable"
    admitted = (
        "GIF interception\n"
        f"guild={guild.id} channel={message.channel.id} message={message.id} "
        f"author={author.id}\n"
        f"jump={jump_url} author_cached={str(author_cached).lower()}\n"
        f"source={event_source} evidence={evidence_source} path={path} "
        f"retention={configured.gif_detector_retention_seconds}s"
    )
    try:
        record = await channel.send(
            f"{admitted}\nstate=admitted",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as error:
        log.warning("Could not publish GIF diagnostic record", exc_info=True)
        try:
            await cog._record_operational_failure(
                guild.id,
                "gif_debug",
                f"Could not publish GIF diagnostic record: {type(error).__name__}: {error}",
            )
        except Exception:
            log.warning("Could not record GIF diagnostic publication failure", exc_info=True)
        return None
    return _ShotDiagnostic(
        cog=cog,
        record=record,
        admitted=admitted,
        guild_id=guild.id,
    )


async def _finish_diagnostic(
    diagnostic: Any,
    *,
    state: str,
    source_delete: str,
    response_cleanup: str,
) -> None:
    if isinstance(diagnostic, asyncio.Task):
        try:
            diagnostic = await diagnostic
        except Exception:
            log.warning("GIF diagnostic admission task failed", exc_info=True)
            return
    if diagnostic is not None:
        await diagnostic.finish(
            state=state,
            source_delete=source_delete,
            response_cleanup=response_cleanup,
        )


async def _cleanup_cancelled_run(
    cog: Any,
    message: Any,
    warning: Any,
    *,
    source_deleted: bool,
) -> None:
    if not source_deleted:
        await _delete_message(cog, message, "Could not delete cancelled GIF message")
    if warning is not None:
        await _delete_message(cog, warning, "Could not delete cancelled GIF warning")


async def _run_secondary(
    cog: Any,
    message: Any,
    text: str,
    retention_seconds: int,
    diagnostic: _ShotDiagnostic | None = None,
) -> None:
    warning = None
    source_deleted = False
    source_outcome = "pending"
    response_outcome = "not_created"
    final_state = "completed"
    try:
        try:
            warning = await message.channel.send(
                f"{message.author.mention} {text}",
                allowed_mentions=_author_mentions(message),
            )
            response_outcome = "pending"
        except discord.HTTPException as error:
            await _record_http_failure(cog, message, "Could not send GIF warning", error)
        if retention_seconds:
            await asyncio.sleep(retention_seconds)
        source_outcome = await _delete_message(
            cog, message, "Could not delete GIF message"
        )
        source_deleted = source_outcome in {"deleted", "already_missing"}
        if warning is not None:
            warning_remaining_seconds = max(5 - retention_seconds, 0)
            if warning_remaining_seconds:
                await asyncio.sleep(warning_remaining_seconds)
            response_outcome = await _delete_message(
                cog, warning, "Could not delete GIF warning"
            )
    except asyncio.CancelledError:
        final_state = "cancelled"
        await _cleanup_cancelled_run(
            cog,
            message,
            warning,
            source_deleted=source_deleted,
        )
        raise
    except BaseException:
        final_state = "failed"
        raise
    finally:
        await _finish_diagnostic(
            diagnostic,
            state=final_state,
            source_delete=source_outcome,
            response_cleanup=response_outcome,
        )


def _author_mentions(message: Any) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[message.author],
        replied_user=False,
    )


async def _show_impact(cog: Any, message: Any, warning: Any) -> Any | None:
    try:
        await warning.edit(
            content="──────────💥",
            allowed_mentions=_author_mentions(message),
        )
    except discord.NotFound:
        return None
    except discord.HTTPException as error:
        await _record_http_failure(
            cog, message, "Could not edit animated GIF warning", error
        )
    return warning


async def _sleep_until(deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)


async def _impact_and_delete_source(
    cog: Any,
    message: Any,
    warning: Any,
) -> tuple[Any | None, str]:
    delete_task = asyncio.create_task(
        _delete_message(cog, message, "Could not delete GIF message")
    )
    impact_task = asyncio.create_task(_show_impact(cog, message, warning))
    delete_result, impact_result = await asyncio.gather(
        delete_task,
        impact_task,
        return_exceptions=True,
    )
    for result in (delete_result, impact_result):
        if isinstance(result, BaseException):
            raise result
    return impact_result, delete_result


async def _animate_warning(
    cog: Any,
    message: Any,
    warning: Any,
    *,
    started_at: float,
    impact_deadline: float,
    retention_seconds: int,
) -> Any | None:
    for frame_second in range(1, min(retention_seconds, 5)):
        await _sleep_until(started_at + frame_second)
        if time.monotonic() >= impact_deadline:
            break
        try:
            await asyncio.wait_for(
                warning.edit(
                    content=render_icbm_frame(
                        message.author.mention,
                        rocket_position=frame_second * 2,
                    ),
                    allowed_mentions=_author_mentions(message),
                ),
                timeout=max(impact_deadline - time.monotonic(), 0),
            )
        except asyncio.TimeoutError:
            break
        except discord.NotFound:
            return None
        except discord.HTTPException as error:
            await _record_http_failure(
                cog, message, "Could not edit animated GIF warning", error
            )
    return warning


async def _run_animated(
    cog: Any,
    message: Any,
    retention_seconds: int,
    diagnostic: _ShotDiagnostic | None = None,
) -> None:
    guild_id = message.guild.id
    warning = None
    source_deleted = False
    source_outcome = "pending"
    response_outcome = "not_created"
    final_state = "completed"
    started_at = time.monotonic()
    impact_deadline = started_at + retention_seconds
    try:
        try:
            warning = await message.channel.send(
                render_icbm_frame(message.author.mention, rocket_position=0),
                allowed_mentions=_author_mentions(message),
            )
            response_outcome = "pending"
        except discord.HTTPException as error:
            await _record_http_failure(
                cog, message, "Could not send animated GIF warning", error
            )

        if warning is not None:
            warning = await _animate_warning(
                cog,
                message,
                warning,
                started_at=started_at,
                impact_deadline=impact_deadline,
                retention_seconds=retention_seconds,
            )

        await _sleep_until(impact_deadline)
        if warning is None:
            source_outcome = await _delete_message(
                cog, message, "Could not delete GIF message"
            )
        else:
            warning, source_outcome = await _impact_and_delete_source(
                cog, message, warning
            )
        source_deleted = source_outcome in {"deleted", "already_missing"}
        if warning is not None:
            await asyncio.sleep(3)
            response_outcome = await _delete_message(
                cog, warning, "Could not delete animated GIF warning"
            )
        elif response_outcome == "pending":
            response_outcome = "already_missing"
    except asyncio.CancelledError:
        final_state = "cancelled"
        await _cleanup_cancelled_run(
            cog,
            message,
            warning,
            source_deleted=source_deleted,
        )
        raise
    except BaseException:
        final_state = "failed"
        raise
    finally:
        cog._gif_detector_animated_guilds.discard(guild_id)
        await _finish_diagnostic(
            diagnostic,
            state=final_state,
            source_delete=source_outcome,
            response_cleanup=response_outcome,
        )


def _remember_message(cog: Any, key: tuple[int, int]) -> bool:
    if key in cog._gif_detector_seen_messages:
        return False
    cog._gif_detector_seen_messages[key] = None
    if len(cog._gif_detector_seen_messages) > SEEN_MESSAGE_LIMIT:
        oldest = next(iter(cog._gif_detector_seen_messages))
        del cog._gif_detector_seen_messages[oldest]
    return True


def _spawn(cog: Any, coroutine: Any) -> None:
    task = asyncio.create_task(coroutine)
    cog._gif_detector_tasks.add(task)

    def settled(done: asyncio.Task) -> None:
        cog._gif_detector_tasks.discard(done)
        cog._observe_background_task(done, "GIF detector task")

    task.add_done_callback(settled)


async def _record_mute_failure(cog: Any, guild_id: int, summary: str) -> None:
    await cog._record_operational_failure(guild_id, "gif_detector", summary)


async def _resolve_mute_member(guild: Any, author: Any) -> Any:
    get_member = getattr(guild, "get_member", None)
    if not callable(get_member):
        return author
    member = get_member(author.id)
    if member is not None:
        return member
    fetch_member = getattr(guild, "fetch_member", None)
    if not callable(fetch_member):
        return author
    return await fetch_member(author.id)


async def _apply_gif_mute(
    cog: Any,
    message: Any,
    key: tuple[int, int],
    duration_seconds: int,
) -> None:
    guild = message.guild
    try:
        mutes = cog.bot.get_cog("Mutes")
        if mutes is None:
            await _record_mute_failure(cog, guild.id, "Core Mutes cog is unavailable")
            return

        mute_config = getattr(mutes, "config", None)
        if mute_config is None:
            await _record_mute_failure(cog, guild.id, "Core Mutes configuration is unavailable")
            return
        role_id = await mute_config.guild(guild).mute_role()
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            await _record_mute_failure(cog, guild.id, "Core Mutes role is not configured")
            return

        author = getattr(guild, "me", None)
        if author is None:
            await _record_mute_failure(cog, guild.id, "Bot member is unavailable for GIF mute")
            return

        member = await _resolve_mute_member(guild, message.author)
        until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
        reason = "GIF defense system activated, ICBM launch privileges revoked"
        response = await mutes.mute_user(
            guild,
            author,
            member,
            until=until,
            reason=reason,
        )
        if not getattr(response, "success", False):
            rejection_reason = (
                getattr(response, "reason", None) or "Core Mutes rejected GIF mute"
            )
            await _record_mute_failure(cog, guild.id, str(rejection_reason))
            return

        async with cog._gif_detector_rate_lock:
            cog._gif_detector_active_mutes[key] = time.monotonic() + duration_seconds

        try:
            await modlog.create_case(
                cog.bot,
                guild,
                message.created_at,
                "smute",
                member,
                author,
                reason,
                until=until,
                channel=None,
            )
        except Exception as error:
            await _record_mute_failure(
                cog,
                guild.id,
                "GIF mute was applied, but its modlog case could not be created: "
                f"{type(error).__name__}: {error}",
            )
    except discord.HTTPException as error:
        await _record_mute_failure(
            cog,
            guild.id,
            f"Could not apply GIF mute: {type(error).__name__}: {error}",
        )
    except Exception as error:
        await _record_mute_failure(
            cog,
            guild.id,
            f"Core Mutes integration failed: {type(error).__name__}: {error}",
        )
    finally:
        async with cog._gif_detector_rate_lock:
            cog._gif_detector_mutes_in_flight.discard(key)


async def _record_gif_hit(cog: Any, message: Any, configured: GuildSettings) -> None:
    key = (message.guild.id, message.author.id)
    now = time.monotonic()
    async with cog._gif_detector_rate_lock:
        cutoff = now - configured.gif_detector_window_seconds
        for stale_key, stale_hits in tuple(cog._gif_detector_hits.items()):
            if stale_key[0] == message.guild.id and (
                not stale_hits or stale_hits[-1] < cutoff
            ):
                del cog._gif_detector_hits[stale_key]
        for stale_key, mute_until in tuple(cog._gif_detector_active_mutes.items()):
            if mute_until <= now:
                del cog._gif_detector_active_mutes[stale_key]

        active_until = cog._gif_detector_active_mutes.get(key)
        if active_until is not None:
            return
        if key in cog._gif_detector_mutes_in_flight:
            return

        hits = cog._gif_detector_hits.setdefault(key, deque())
        hits.append(now)
        if len(hits) < configured.gif_detector_threshold:
            return

        del cog._gif_detector_hits[key]
        cog._gif_detector_mutes_in_flight.add(key)
        _spawn(
            cog,
            _apply_gif_mute(
                cog,
                message,
                key,
                configured.gif_detector_mute_duration_seconds,
            ),
        )


async def _eligible_settings(cog: Any, message: Any) -> GuildSettings | None:
    guild = message.guild
    if guild is None or message.author.bot or message.webhook_id is not None:
        return None
    if await cog.bot.cog_disabled_in_guild(cog, guild):
        return None
    configured = GuildSettings.from_mapping(await cog.config.guild(guild).all())
    if not configured.gif_detector_enabled:
        return None
    if channel_scope_id(message.channel) not in configured.gif_detector_channels:
        return None
    if await cog._is_protected_member(message.author, guild):
        return None
    return configured


async def _admit_message(
    cog: Any,
    message: Any,
    *,
    event_source: str,
    evidence_source: str,
) -> None:
    configured = await _eligible_settings(cog, message)
    if configured is None:
        return
    guild = message.guild
    if not _remember_message(cog, (guild.id, message.id)):
        return
    animated = (
        configured.gif_detector_animation_enabled
        and guild.id not in cog._gif_detector_animated_guilds
    )
    path = "animated" if animated else "secondary"
    diagnostic = asyncio.create_task(
        _start_diagnostic(
            cog,
            message,
            configured,
            event_source=event_source,
            evidence_source=evidence_source,
            path=path,
        )
    )
    if animated:
        cog._gif_detector_animated_guilds.add(guild.id)
        _spawn(
            cog,
            _run_animated(
                cog,
                message,
                configured.gif_detector_retention_seconds,
                diagnostic,
            ),
        )
    else:
        _spawn(
            cog,
            _run_secondary(
                cog,
                message,
                configured.gif_detector_secondary_message,
                configured.gif_detector_retention_seconds,
                diagnostic,
            ),
        )
    await _record_gif_hit(cog, message, configured)


async def on_message(cog: Any, message: Any) -> bool:
    evidence_source = gif_evidence_source(
        embeds=getattr(message, "embeds", ()),
        attachments=getattr(message, "attachments", ()),
        content=getattr(message, "content", ""),
    )
    if evidence_source is not None:
        await _admit_message(
            cog,
            message,
            event_source="create",
            evidence_source=evidence_source,
        )
    return evidence_source is not None


async def schedule_remote_media_fallback(
    cog: Any,
    message: Any,
    *,
    candidate: str | None = None,
    event_source: str = "create",
) -> None:
    """Schedule final remote animation classification for supported images."""

    message_candidates = tuple(_remote_media_candidates(message))
    candidates = _prioritize_remote_media_candidates(
        (candidate, *message_candidates) if candidate is not None else message_candidates
    )
    if not candidates:
        return
    if await _eligible_settings(cog, message) is None:
        return
    key = (message.guild.id, message.id)
    async with cog._gif_detector_rate_lock:
        if key in cog._gif_detector_remote_media_in_flight:
            return
        cog._gif_detector_remote_media_in_flight.add(key)

    async def inspect() -> None:
        try:
            animated_candidate = None
            for inspected_candidate in candidates:
                if (
                    await cog._gif_detector_remote_inspector.inspect(
                        inspected_candidate
                    )
                    is True
                ):
                    animated_candidate = inspected_candidate
                    break
            if animated_candidate is not None:
                try:
                    current_message = await message.channel.fetch_message(message.id)
                except discord.NotFound:
                    return
                except discord.HTTPException as error:
                    await _record_http_failure(
                        cog, message, "Could not recheck remote image source", error
                    )
                    return
                if not any(
                    _same_remote_media_candidate(animated_candidate, current_candidate)
                    for current_candidate in _remote_media_candidates(current_message)
                ):
                    return
                await _admit_message(
                    cog,
                    current_message,
                    event_source=event_source,
                    evidence_source="remote_media",
                )
        finally:
            async with cog._gif_detector_rate_lock:
                cog._gif_detector_remote_media_in_flight.discard(key)

    _spawn(cog, inspect())


async def on_raw_message_edit(cog: Any, payload: Any) -> None:
    raw_data = payload.data if isinstance(getattr(payload, "data", None), Mapping) else {}
    local_evidence_source = gif_evidence_source(
        embeds=raw_data.get("embeds", ()),
        attachments=raw_data.get("attachments", ()),
        content=raw_data.get("content", ""),
    )
    remote_media_candidates = tuple(
        _remote_media_candidates_from_fields(
            embeds=raw_data.get("embeds", ()),
            attachments=raw_data.get("attachments", ()),
            content=raw_data.get("content", ""),
        )
    )
    remote_media_candidate = next(iter(remote_media_candidates), None)
    if local_evidence_source is None and remote_media_candidate is None:
        return

    updated_message = getattr(payload, "message", None)
    if updated_message is not None:
        if local_evidence_source is not None:
            await _admit_message(
                cog,
                updated_message,
                event_source="raw-edit",
                evidence_source=local_evidence_source,
            )
        else:
            await schedule_remote_media_fallback(
                cog,
                updated_message,
                candidate=remote_media_candidate,
                event_source="raw-edit",
            )
        return

    channel_id = getattr(payload, "channel_id", None)
    message_id = getattr(payload, "message_id", None)
    if channel_id is None or message_id is None:
        return

    try:
        channel = cog.bot.get_channel(channel_id)
        if channel is None:
            channel = await cog.bot.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return
    except discord.HTTPException as error:
        guild_id = getattr(payload, "guild_id", None)
        if guild_id is not None:
            await cog._record_operational_failure(
                guild_id,
                "gif_detector",
                f"Could not fetch edited GIF message: {type(error).__name__}: {error}",
            )
        return
    if local_evidence_source is not None:
        await _admit_message(
            cog,
            message,
            event_source="raw-edit",
            evidence_source=local_evidence_source,
        )
    else:
        await schedule_remote_media_fallback(
            cog,
            message,
            candidate=remote_media_candidate,
            event_source="raw-edit",
        )


def _format_channels(cog: Any, guild: Any, channel_ids: list[int]) -> str:
    if not channel_ids:
        return _("Not configured")
    labels = []
    for channel_id in channel_ids[:20]:
        labels.append(cog._format_channel_setting(guild, channel_id))
    remaining = len(channel_ids) - len(labels)
    if remaining:
        labels.append(_("… and {count} more").format(count=remaining))
    return "\n".join(labels)


async def config_gif_detector(cog: Any, ctx: commands.Context) -> None:
    configured = GuildSettings.from_mapping(await cog.config.guild(ctx.guild).all())
    await cog._send_config_dump(
        ctx,
        _("GIF detector config"),
        [
            (_("Enabled"), cog._format_bool_setting(configured.gif_detector_enabled)),
            (
                _("Debug logging"),
                cog._format_bool_setting(configured.gif_detector_debug_enabled),
            ),
            (
                _("Debug channel"),
                cog._format_channel_setting(
                    ctx.guild, configured.gif_detector_debug_channel
                ),
            ),
            (
                _("Animation"),
                cog._format_bool_setting(configured.gif_detector_animation_enabled),
            ),
            (
                _("Channels"),
                _format_channels(cog, ctx.guild, configured.gif_detector_channels),
            ),
            (_("Secondary message"), configured.gif_detector_secondary_message),
            (
                _("GIF retention"),
                _("{seconds} seconds").format(
                    seconds=configured.gif_detector_retention_seconds
                ),
            ),
            (_("Mute threshold"), str(configured.gif_detector_threshold)),
            (
                _("Rolling window"),
                _("{seconds} seconds").format(
                    seconds=configured.gif_detector_window_seconds
                ),
            ),
            (
                _("Mute duration"),
                _("{seconds} seconds").format(
                    seconds=configured.gif_detector_mute_duration_seconds
                ),
            ),
        ],
    )


async def gif_detector_toggle(cog: Any, ctx: commands.Context, value: bool) -> None:
    await cog.config.guild(ctx.guild).gif_detector_enabled.set(value)
    await ctx.send(_("✅ GIF detector enabled: {value}").format(value=str(value).lower()))


async def gif_detector_debug_toggle(
    cog: Any, ctx: commands.Context, value: bool
) -> None:
    await cog.config.guild(ctx.guild).gif_detector_debug_enabled.set(value)
    await ctx.send(
        _("✅ GIF detector debug logging enabled: {value}").format(
            value=str(value).lower()
        )
    )


async def gif_detector_animation(cog: Any, ctx: commands.Context, value: bool) -> None:
    await cog.config.guild(ctx.guild).gif_detector_animation_enabled.set(value)
    await ctx.send(_("✅ GIF detector animation enabled: {value}").format(value=str(value).lower()))


async def gif_detector_retention(
    cog: Any, ctx: commands.Context, seconds: int | None = None
) -> None:
    await _configure_bounded_integer(
        cog,
        ctx,
        key="gif_detector_retention_seconds",
        value=seconds,
        minimum=GIF_RETENTION_MIN_SECONDS,
        maximum=GIF_RETENTION_MAX_SECONDS,
        label=_("GIF retention in seconds"),
    )


async def _configure_bounded_integer(
    cog: Any,
    ctx: commands.Context,
    *,
    key: str,
    value: int | None,
    minimum: int,
    maximum: int,
    label: str,
) -> None:
    setting = getattr(cog.config.guild(ctx.guild), key)
    if value is None:
        current = await setting()
        await ctx.send(_("{label}: {value}").format(label=label, value=current))
        return
    if not minimum <= value <= maximum:
        raise commands.UserFeedbackCheckFailure(
            _("Value must be between {minimum} and {maximum}.").format(
                minimum=minimum,
                maximum=maximum,
            )
        )
    await setting.set(value)
    await ctx.send(_("✅ {label} set to {value}").format(label=label, value=value))


async def gif_detector_threshold(
    cog: Any, ctx: commands.Context, value: int | None = None
) -> None:
    await _configure_bounded_integer(
        cog,
        ctx,
        key="gif_detector_threshold",
        value=value,
        minimum=GIF_THRESHOLD_MIN,
        maximum=GIF_THRESHOLD_MAX,
        label=_("GIF mute threshold"),
    )


async def gif_detector_window(
    cog: Any, ctx: commands.Context, seconds: int | None = None
) -> None:
    await _configure_bounded_integer(
        cog,
        ctx,
        key="gif_detector_window_seconds",
        value=seconds,
        minimum=GIF_WINDOW_MIN_SECONDS,
        maximum=GIF_WINDOW_MAX_SECONDS,
        label=_("GIF rolling window in seconds"),
    )


async def gif_detector_mute_duration(
    cog: Any, ctx: commands.Context, seconds: int | None = None
) -> None:
    await _configure_bounded_integer(
        cog,
        ctx,
        key="gif_detector_mute_duration_seconds",
        value=seconds,
        minimum=GIF_MUTE_MIN_SECONDS,
        maximum=GIF_MUTE_MAX_SECONDS,
        label=_("GIF mute duration in seconds"),
    )


async def gif_detector_message_set(
    cog: Any, ctx: commands.Context, *, text: str
) -> None:
    text = text.strip()
    if not text or len(text) > MAX_SECONDARY_MESSAGE_LENGTH:
        raise commands.UserFeedbackCheckFailure(
            _("Message must contain between 1 and {limit} characters.").format(
                limit=MAX_SECONDARY_MESSAGE_LENGTH
            )
        )
    await cog.config.guild(ctx.guild).gif_detector_secondary_message.set(text)
    await ctx.send(_("✅ Secondary GIF warning updated."))


async def gif_detector_message_reset(cog: Any, ctx: commands.Context) -> None:
    default = str(settings.DEFAULTS["gif_detector_secondary_message"])
    await cog.config.guild(ctx.guild).gif_detector_secondary_message.set(default)
    await ctx.send(_("✅ Secondary GIF warning reset to `{message}`").format(message=default))
