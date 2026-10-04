from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
import typing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import discord
from redbot.core import commands
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import box, pagify

from . import channel_routing
from .remote_media import media_decoder_support
from .settings import (
    CORE_ACTION_OPTIONS,
    DEFAULT_STATS,
    MAX_MANUAL_PUNISHMENT_ROLES_PER_CHANNEL,
    GuildSettings,
)

_ = Translator("Honeypot", __file__)
log = logging.getLogger("red.Honeypot")

@dataclass(frozen=True, slots=True)
class DoctorResult:
    name: str
    status: typing.Literal["healthy", "warning", "failed"]
    detail: str = ""


DoctorCheck = typing.Callable[[], typing.Awaitable[typing.Sequence[DoctorResult]]]


async def config_dump(
    cog,
    ctx: commands.Context,
    config_sender: typing.Callable[..., typing.Awaitable[None]],
) -> None:
    """Show current honeypot configuration by section."""
    return await cog._send_group_overview(ctx, config_sender)


async def honeypot_mod_stats(cog, ctx: commands.Context) -> None:
    """Show detailed moderation statistics."""
    stats = DEFAULT_STATS.copy()
    stats.update(await cog.config.guild(ctx.guild).stats())
    pending_joinwatch_assignments = await cog.config.guild(
        ctx.guild
    ).joinwatch_pending_role_assignments()
    pending_joinwatch_roles = await cog.config.guild(ctx.guild).joinwatch_pending_roles()
    now = datetime.now(timezone.utc)
    case_counts = await asyncio.to_thread(
        cog._case_store.operational_counts,
        ctx.guild.id,
        now,
        now - timedelta(minutes=5),
    )
    total_joins = stats["joinwatch_total_joins"]
    young_joins = stats["joinwatch_young_joins"]
    young_join_rate = (young_joins / total_joins * 100) if total_joins else 0
    sections = {
        "Detection": {
            "Total detections": stats["detections"],
            "Suspicious detections": stats["suspicious"],
            "Whitelisted users": stats["whitelisted"],
            "Purged messages": stats["purged_messages"],
            "Cached purge deletes": stats["cached_purge_deletes"],
            "Forward purge deletes": stats["forward_purge_deletes"],
            "Forward purge delete failures": stats["forward_purge_delete_failures"],
            "Evidence capture failures": stats["evidence_capture_failures"],
            "Active detection cases": case_counts["active_cases"],
            "Due detection cases": case_counts["due_cases"],
            "Stale resolving cases": case_counts["stale_resolving_cases"],
            "Failed containment cases": case_counts["failed_containment"],
            "Forbidden message deletes": case_counts["forbidden_deletes"],
            "Outstanding durable operations": case_counts["outstanding_operations"],
            "Queued privacy deletions": case_counts["privacy_deletion_jobs"],
        },
        "Honeypot": {
            "Honeypot hits": stats["honeypot_hits"],
            "Honeypot reviews": stats["honeypot_reviews"],
            "Honeypot kicks": stats["honeypot_kicks"],
            "Honeypot bans": stats["honeypot_bans"],
            "Honeypot catches": stats["honeypot_catches"],
        },
        "Spam": {
            "Spam hits": stats["spam_hits"],
            "Spam reviews": stats["spam_reviews"],
            "Spam kicks": stats["spam_kicks"],
            "Spam bans": stats["spam_bans"],
            "Spam catches": stats["spam_catches"],
        },
        "Image detection": {
            "Image hits": stats["image_hits"],
            "Image reviews": stats["image_reviews"],
            "Image kicks": stats["image_kicks"],
            "Image bans": stats["image_bans"],
            "Image catches": stats["image_catches"],
        },
        "Review": {
            "Reviews sent": stats["reviewed"],
            "Expired reviews": stats["review_expired"],
            "Ignored reviews": stats["ignored"],
            "Applied temporary mutes": stats["pending_mutes"],
            "Failed temporary mutes": stats["pending_mute_failures"],
        },
        "Joinwatch": {
            "Total joins": total_joins,
            "Young joins": young_joins,
            "Young join rate": f"{young_join_rate:.1f}%",
            "Auto-role applications scheduled": stats["joinwatch_auto_roles_scheduled"],
            "Pending role applications": len(pending_joinwatch_assignments),
            "Auto-roles applied": stats["joinwatch_auto_roles"],
            "Auto-role failures": stats["joinwatch_auto_role_failures"],
            "Auto-roles cleared": stats["joinwatch_auto_roles_cleared"],
            "Active auto-role timers": len(pending_joinwatch_roles),
            "Auto-role punishments": stats["joinwatch_auto_role_punishments"],
        },
        "Actions": {
            "Kicked users": stats["kicked"],
            "Banned users": stats["banned"],
            "Failed actions": stats["failed_actions"],
            "Dry-run actions": stats["dry_run_actions"],
        },
    }
    lines = []
    for section, values in sections.items():
        if lines:
            lines.append("")
        lines.append(f"{section}:")
        lines.extend(f"  {label}: {value}" for label, value in values.items())
    await ctx.send(_("**Honeypot stats:**\n") + box("\n".join(lines)))


async def honeypot_stats(cog, ctx: commands.Context) -> None:
    """Show public server safety statistics."""
    stats = DEFAULT_STATS.copy()
    stats.update(await cog.config.guild(ctx.guild).stats())
    detected_activity = stats["detections"]
    moderation_actions = stats["kicked"] + stats["banned"]
    automated_protections = stats["joinwatch_auto_roles"] + stats["joinwatch_auto_role_punishments"]
    lines = [
        f"  {_('Detected activity')}: {detected_activity}",
        f"  {_('Moderation actions')}: {moderation_actions}",
        f"  {_('Sent for review')}: {stats['reviewed']}",
        f"  {_('Automated protections')}: {automated_protections}",
    ]
    await ctx.send(_("**Server safety stats:**\n") + box("\n".join(lines)))


async def honeypot_reset_stats(cog, ctx: commands.Context) -> None:
    """Reset stored honeypot statistics."""
    await cog.config.guild(ctx.guild).stats.set(DEFAULT_STATS.copy())
    await ctx.send(_("✅ Stats reset"))


def _verify_detection_case_evidence_directory(cog) -> None:
    probe_path: Path | None = None
    probe_error: OSError | None = None
    try:
        cog._detection_case_files_path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=cog._detection_case_files_path,
            prefix=".doctor-",
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            probe.write(b"ok")
        if probe_path.read_bytes() != b"ok":
            raise OSError("evidence directory read/write check failed")
    except OSError as error:
        probe_error = error
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                if probe_error is None:
                    probe_error = cleanup_error
    if probe_error is not None:
        raise probe_error


async def _doctor_runtime_checks(cog, guild_id: int) -> tuple[DoctorResult, ...]:
    results: list[DoctorResult] = []
    case_database_ok = True
    try:
        await asyncio.to_thread(cog._case_store.verify_read_write)
    except (OSError, sqlite3.Error) as error:
        case_database_ok = False
        results.append(
            DoctorResult(
                "Detection case database",
                "failed",
                f"Read/write check failed: {error}",
            )
        )
    else:
        results.append(DoctorResult("Detection case database", "healthy"))
    try:
        await asyncio.to_thread(_verify_detection_case_evidence_directory, cog)
    except OSError as error:
        results.append(
            DoctorResult(
                "Detection case evidence directory",
                "failed",
                f"Read/write check failed: {error}",
            )
        )
    else:
        results.append(DoctorResult("Detection case evidence directory", "healthy"))
    if not case_database_ok:
        return tuple(results)

    now = datetime.now(timezone.utc)
    case_counts = await asyncio.to_thread(
        cog._case_store.operational_counts,
        guild_id,
        now,
        now - timedelta(minutes=5),
    )
    for name, count, detail in (
        (
            "Due detection cases",
            case_counts["due_cases"],
            "Run detection case reconciliation.",
        ),
        (
            "Stale resolving cases",
            case_counts["stale_resolving_cases"],
            "Run detection case reconciliation.",
        ),
        (
            "Failed containment cases",
            case_counts["failed_containment"],
            "Inspect moderation case delete failures.",
        ),
    ):
        results.append(
            DoctorResult(
                f"{name}: {count}",
                "healthy" if count == 0 else "failed",
                detail,
            )
        )
    return tuple(results)


async def _doctor_bait_role_collision_checks(
    cog,
    guild_id: int,
    guild_settings: GuildSettings,
) -> tuple[DoctorResult, ...]:
    bait_role_id = guild_settings.baitrole_id
    if bait_role_id is None:
        return ()

    results: list[DoctorResult] = []
    if bait_role_id == guild_settings.mute_role:
        results.append(
            DoctorResult(
                "Bait role reuses the mute role",
                "warning",
                "Configure a dedicated bait role.",
            )
        )
    if bait_role_id == guild_settings.joinwatch_auto_role_id:
        results.append(
            DoctorResult(
                "Bait role reuses the Joinwatch auto-role",
                "warning",
                "Configure a dedicated bait role.",
            )
        )
    get_cog = getattr(cog.bot, "get_cog", None)
    nhmisc = get_cog("NHMisc") if callable(get_cog) else None
    configured_sticky_role_ids = getattr(nhmisc, "configured_sticky_role_ids", None)
    if callable(configured_sticky_role_ids):
        sticky_role_ids = await configured_sticky_role_ids(guild_id)
        if bait_role_id in sticky_role_ids:
            results.append(
                DoctorResult(
                    "Bait role reuses an NHMisc sticky role",
                    "warning",
                    "Configure a dedicated bait role.",
                )
            )
    return tuple(results)


def _doctor_gif_detector_checks(
    cog,
    guild,
    me,
    guild_settings: GuildSettings,
) -> tuple[DoctorResult, ...]:
    if not guild_settings.gif_detector_enabled:
        return ()
    results: list[DoctorResult] = []
    for format_name, available in media_decoder_support().items():
        results.append(
            DoctorResult(
                f"{format_name} decoder is "
                f"{'available' if available else 'unavailable'}",
                "healthy" if available else "failed",
                "Reinstall the Honeypot requirements and reload the cog."
                if not available
                else "",
            )
        )
    for channel_id in guild_settings.gif_detector_channels:
        channel = cog._get_text_channel_or_thread(guild, channel_id)
        if channel is None:
            results.append(
                DoctorResult(
                    "GIF detector channel is missing",
                    "failed",
                    "Run `honeypot gifdetector channel remove` and add a valid channel.",
                )
            )
            continue
        permissions = channel.permissions_for(me)
        if not getattr(permissions, "send_messages", False):
            results.append(
                DoctorResult(
                    f"GIF detector cannot send messages in {channel.mention}",
                    "failed",
                    "Grant Send Messages.",
                )
            )
        if not getattr(permissions, "send_messages_in_threads", False):
            results.append(
                DoctorResult(
                    "GIF detector cannot send messages in threads under "
                    f"{channel.mention}",
                    "failed",
                    "Grant Send Messages in Threads.",
                )
            )
        if not getattr(permissions, "manage_messages", False):
            results.append(
                DoctorResult(
                    f"GIF detector cannot manage messages in {channel.mention}",
                    "failed",
                    "Grant Manage Messages.",
                )
            )
    return tuple(results)


async def _doctor_configuration_checks(
    cog,
    guild,
    me,
    guild_settings: GuildSettings,
    honeypot_channels: typing.Sequence,
) -> tuple[DoctorResult, ...]:
    results: list[DoctorResult] = []
    if not guild_settings.enabled:
        results.append(DoctorResult("Honeypot is disabled.", "warning"))
    if guild_settings.action is None:
        results.append(
            DoctorResult(
                "Honeypot action is invalid",
                "failed",
                "Run `honeypot honeypot action`.",
            )
        )
    if guild_settings.spam_action.value not in CORE_ACTION_OPTIONS:
        results.append(
            DoctorResult(
                "Spam action is invalid",
                "failed",
                "Run `honeypot spam action`.",
            )
        )
    if guild_settings.enabled and not honeypot_channels:
        results.append(
            DoctorResult(
                "No honeypot channel exists",
                "failed",
                "Run `honeypot channels honeypot add`.",
            )
        )
    results.extend(await _doctor_bait_role_collision_checks(cog, guild.id, guild_settings))
    if guild_settings.mute_role:
        mute_role = guild.get_role(guild_settings.mute_role)
        if mute_role is None:
            results.append(
                DoctorResult(
                    "Mute role is missing",
                    "failed",
                    "Run `honeypot punishment mute_role`.",
                )
            )
        elif not me.top_role > mute_role:
            results.append(
                DoctorResult(
                    "Bot is not above mute role",
                    "failed",
                    "Move bot role above mute role.",
                )
            )
    role_nt_channel_counts: dict[int, int] = {}
    for entry in guild_settings.manual_punishment_roles.values():
        role = guild.get_role(entry.role_id)
        if role is None:
            results.append(
                DoctorResult(
                    f"Role n’t role is missing: {entry.role_id}",
                    "failed",
                    "Remove or reconfigure the Role n’t entry.",
                )
            )
        elif getattr(role, "managed", False) or not me.top_role > role:
            results.append(
                DoctorResult(
                    f"Role n’t role is not manageable: {getattr(role, 'name', entry.role_id)}",
                    "failed",
                    "Use a role below the bot's top role.",
                )
            )
        for channel_id in entry.source_channel_ids:
            role_nt_channel_counts[channel_id] = (
                role_nt_channel_counts.get(channel_id, 0) + 1
            )
            if guild.get_channel(channel_id) is None:
                results.append(
                    DoctorResult(
                        f"Role n’t source channel is missing: {channel_id}",
                        "failed",
                        "Remove the missing source channel from the Role n’t entry.",
                    )
                )
        notification_channel_id = entry.notification_channel_id
        if (
            notification_channel_id is not None
            and guild.get_channel(notification_channel_id) is None
        ):
            results.append(
                DoctorResult(
                    f"Role n’t notification channel is missing: {notification_channel_id}",
                    "failed",
                    "Clear or replace the Role n’t notification channel.",
                )
            )
    for channel_id, role_count in role_nt_channel_counts.items():
        if role_count > MAX_MANUAL_PUNISHMENT_ROLES_PER_CHANNEL:
            results.append(
                DoctorResult(
                    f"Role n’t source channel {channel_id} has {role_count} options",
                    "failed",
                    "Keep at most 25 Role n’t options per source channel.",
                )
            )
    if (
        guild_settings.manual_evidence_channel is not None
        and cog.bot.get_cog("Mutes") is None
    ):
        results.append(
            DoctorResult(
                "Mutes cog is unavailable for manual punishments",
                "warning",
                "Load and configure the core Mutes cog before using Mute.",
            )
        )
    if guild_settings.joinwatch_auto_role_enabled:
        auto_role_id = guild_settings.joinwatch_auto_role_id
        auto_role = guild.get_role(auto_role_id) if auto_role_id else None
        if auto_role is None:
            results.append(
                DoctorResult(
                    "Joinwatch auto-role is missing",
                    "failed",
                    "Run `honeypot joinwatch autorole role`.",
                )
            )
        elif not me.top_role > auto_role:
            results.append(
                DoctorResult(
                    "Bot is not above joinwatch auto-role",
                    "failed",
                    "Move bot role above the joinwatch auto-role.",
                )
            )
    results.extend(_doctor_gif_detector_checks(cog, guild, me, guild_settings))
    if guild_settings.joinwatch_captcha_enabled or guild_settings.joinwatch_groups_enabled or guild_settings.captcha_panel_message_id:
        results.extend(DoctorResult(issue, "failed", "Check `honeypot captcha` configuration.") for issue in await cog._joinwatch_verification.configuration_issues(guild))
    return tuple(results)


def _destination_is_required(key: str, settings: GuildSettings) -> bool:
    review_required = (
        settings.fallback_action.value == "review"
        or settings.review_enabled
        or settings.whitelist_mode.value == "review"
        or settings.spam_enabled
        and settings.spam_action.value == "review"
    )
    return {
        "errors": any(
            (
                settings.enabled,
                settings.spam_enabled,
                settings.imagescan_detector_enabled,
                settings.gif_detector_enabled,
                settings.joinwatch_enabled,
                settings.baitrole_enabled,
            )
        ),
        "review": review_required,
        "joinwatch": settings.joinwatch_enabled
        and settings.joinwatch_alert_enabled,
        "bait_role": settings.baitrole_enabled,
        "gif_debug": settings.gif_detector_debug_enabled,
        "captcha": settings.joinwatch_captcha_enabled or settings.joinwatch_groups_enabled,
        "captcha_log": settings.joinwatch_captcha_enabled or settings.joinwatch_groups_enabled,
    }.get(key, False)


async def _doctor_destination_checks(
    cog,
    guild,
    me,
    settings: GuildSettings,
) -> tuple[DoctorResult, ...]:
    permission_names = {
        "send_messages": ("send_messages", "Send Messages"),
        "read_history": ("read_message_history", "Read Message History"),
        "create_public_threads": ("create_public_threads", "Create Public Threads"),
        "send_in_threads": ("send_messages_in_threads", "Send Messages in Threads"),
        "embed_links": ("embed_links", "Embed Links"),
        "attach_files": ("attach_files", "Attach Files"),
        "manage_threads": ("manage_threads", "Manage Threads"),
    }
    results = []
    for spec in channel_routing.CHANNEL_CATEGORIES:
        if spec.kind != "destination":
            continue
        channel_id = getattr(settings, spec.config_field)
        channel = (
            cog._get_text_channel_or_thread(guild, channel_id)
            if channel_id is not None
            else None
        )
        if channel is None:
            if channel_id is not None or _destination_is_required(spec.key, settings):
                results.append(
                    DoctorResult(
                        f"{spec.label} channel is missing",
                        "failed",
                        f"Run `honeypot channels {spec.central_command}`.",
                    )
                )
            continue
        if not spec.allow_threads and not isinstance(channel, discord.TextChannel):
            results.append(
                DoctorResult(
                    f"{spec.label} must be a normal text channel",
                    "failed",
                    f"Run `honeypot channels {spec.central_command}`.",
                )
            )
            continue
        permissions = channel.permissions_for(me)
        missing = []
        if not getattr(permissions, "view_channel", False):
            missing.append("View Channel")
        for permission in spec.required_permissions:
            attribute, label = permission_names[permission]
            if permission == "send_messages" and isinstance(channel, discord.Thread):
                attribute, label = "send_messages_in_threads", "Send Messages in Threads"
            if not getattr(permissions, attribute, False):
                missing.append(label)
        if missing:
            results.append(
                DoctorResult(
                    f"{spec.label} channel permissions",
                    "failed",
                    "Grant: " + ", ".join(missing),
                )
            )
    return tuple(results)


async def _doctor_channel_permission_checks(
    cog,
    guild,
    me,
    honeypot_channels: typing.Sequence,
    *,
    missing_purge_permissions,
    is_purgeable_message_channel,
) -> tuple[DoctorResult, ...]:
    results: list[DoctorResult] = []
    for honeypot_channel in honeypot_channels:
        perms = honeypot_channel.permissions_for(me)
        missing_permissions = missing_purge_permissions(perms)
        if missing_permissions:
            results.append(
                DoctorResult(
                    f"{honeypot_channel} permissions",
                    "failed",
                    "Missing: " + ", ".join(missing_permissions),
                )
            )
    skipped_channels = []
    purgeable_channels = [
        channel
        for channel in list(guild.channels) + list(guild.threads)
        if is_purgeable_message_channel(channel)
    ]
    for channel in purgeable_channels:
        perms = channel.permissions_for(me)
        if not perms.view_channel:
            continue
        if not perms.manage_messages:
            skipped_channels.append(channel.mention)
    if skipped_channels:
        results.append(
            DoctorResult(
                "Cached purge can delete visible message channels",
                "failed",
                "\nManage - " + ", ".join(skipped_channels),
            )
        )
    return tuple(results)


async def _doctor_guild_permission_checks(
    cog,
    me,
    guild_settings: GuildSettings,
) -> tuple[DoctorResult, ...]:
    results: list[DoctorResult] = []
    guild_perms = me.guild_permissions
    configured_actions = {
        guild_settings.action.value if guild_settings.action is not None else None,
        guild_settings.fallback_action.value,
        guild_settings.spam_action.value,
        guild_settings.imagescan_detector_action.value,
    }
    if "kick" in configured_actions and not guild_perms.kick_members:
        results.append(DoctorResult("Cannot kick members", "failed", "Grant Kick Members."))
    if "ban" in configured_actions and not guild_perms.ban_members:
        results.append(DoctorResult("Cannot ban members", "failed", "Grant Ban Members."))
    roles_configured = guild_settings.mute_role or guild_settings.joinwatch_auto_role_enabled
    if roles_configured and not guild_perms.manage_roles:
        results.append(
            DoctorResult(
                "Cannot manage configured roles",
                "failed",
                "Grant Manage Roles.",
            )
        )
    return tuple(results)


async def _run_doctor_checks(
    checks: typing.Sequence[DoctorCheck],
) -> tuple[DoctorResult, ...]:
    results: list[DoctorResult] = []
    for check in checks:
        results.extend(await check())
    return tuple(results)


def _render_doctor_results(
    results: typing.Sequence[DoctorResult],
) -> tuple[str, ...]:
    failed = [
        f"❌ {result.name}{result.detail}"
        if result.detail.startswith("\n")
        else f"❌ {result.name} - {result.detail}"
        for result in results
        if result.status == "failed"
    ]
    warnings = [
        f"⚠️ {result.name}" if not result.detail else f"⚠️ {result.name} - {result.detail}"
        for result in results
        if result.status == "warning"
    ]
    header = _("**Honeypot doctor:**\n")
    findings = failed + warnings
    body = "\n".join(findings) if findings else "✅ No configuration or runtime problems found."
    return tuple(header + page for page in pagify(body, page_length=2000 - len(header)))


async def honeypot_doctor(cog, ctx: commands.Context) -> None:
    """Check honeypot configuration and required permissions."""
    if not cog._channel_is_private(ctx.guild, ctx.channel):
        await ctx.send("Run this command in a private moderator channel")
        return
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    results = list(
        await _run_doctor_checks([partial(_doctor_runtime_checks, cog, ctx.guild.id)])
    )
    me = ctx.guild.me
    if me is None:
        await ctx.send(_("**Honeypot doctor:**\n❌ I couldn't find my server member"))
        return

    honeypot_channels = tuple(
        channel
        for channel_id in guild_settings.honeypot_channels
        if (channel := cog._get_text_channel_or_thread(ctx.guild, channel_id)) is not None
    )
    checks: list[DoctorCheck] = [
        partial(
            _doctor_configuration_checks,
            cog,
            ctx.guild,
            me,
            guild_settings,
            honeypot_channels,
        ),
        partial(_doctor_destination_checks, cog, ctx.guild, me, guild_settings),
        partial(
            cog._doctor_channel_permission_checks,
            ctx.guild,
            me,
            honeypot_channels,
        ),
        partial(_doctor_guild_permission_checks, cog, me, guild_settings),
    ]
    results.extend(await _run_doctor_checks(checks))
    for page in _render_doctor_results(results):
        await ctx.send(page)
