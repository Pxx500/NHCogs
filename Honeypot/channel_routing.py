"""Canonical channel categories and shared configuration behavior.

Adding a channel category:
1. Reuse an existing semantic category when it fits.
2. Otherwise add one specification to ``CHANNEL_CATEGORIES``.
3. Route publication through that category instead of reading config directly.
4. Add the declared module-facing command alias when applicable.
5. Never add a private setter or cross-category fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import discord
from redbot.core import commands
from redbot.core.i18n import Translator

from .settings import GuildSettings

_ = Translator("Honeypot", __file__)


@dataclass(frozen=True)
class ChannelCategory:
    key: str
    config_field: str
    label: str
    kind: Literal["destination", "scope"]
    cardinality: Literal["single", "multiple"] = "single"
    allow_threads: bool = True
    thread_scope: Literal["self", "parent"] = "self"
    default_to_current_channel: bool = False
    private: bool = False
    required_permissions: tuple[str, ...] = ("send_messages",)
    central_command: str | None = None
    module_command: str | None = None


CHANNEL_CATEGORIES = (
    ChannelCategory(
        "review",
        "review_channel",
        "Review",
        "destination",
        allow_threads=False,
        required_permissions=(
            "send_messages",
            "read_history",
            "create_public_threads",
            "send_in_threads",
            "embed_links",
            "attach_files",
            "manage_threads",
        ),
        central_command="review",
        module_command="review channel",
    ),
    ChannelCategory(
        "errors",
        "errors_channel",
        "Errors",
        "destination",
        central_command="errors",
    ),
    ChannelCategory(
        "daily_stats",
        "daily_stats_channel",
        "Daily stats",
        "destination",
        required_permissions=("send_messages", "embed_links"),
        central_command="daily-stats",
        module_command="stats channel",
    ),
    ChannelCategory(
        "manual_evidence",
        "manual_evidence_channel",
        "Manual evidence",
        "destination",
        allow_threads=False,
        private=True,
        required_permissions=(
            "send_messages",
            "read_history",
            "embed_links",
            "attach_files",
        ),
        central_command="manual-evidence",
        module_command="evidence channel",
    ),
    ChannelCategory(
        "joinwatch",
        "joinwatch_channel",
        "JoinWatch",
        "destination",
        central_command="joinwatch",
        module_command="joinwatch channel",
    ),
    ChannelCategory(
        "bait_role",
        "baitrole_channel",
        "Bait role",
        "destination",
        required_permissions=("send_messages", "embed_links"),
        central_command="bait-role",
        module_command="bait_role channel",
    ),
    ChannelCategory(
        "gif_debug",
        "gif_detector_debug_channel",
        "GIF debug",
        "destination",
        central_command="gif-debug",
        module_command="gifdetector debug channel",
    ),
    ChannelCategory(
        "mement_notifications",
        "manual_evidence_mement_notification_channel",
        "Memen't notifications",
        "destination",
        allow_threads=False,
        central_command="mement-notifications",
        module_command="evidence mement_notification_channel",
    ),
    ChannelCategory(
        "honeypot_scope",
        "honeypot_channels",
        "Honeypot scope",
        "scope",
        cardinality="multiple",
        required_permissions=("read_history", "manage_messages"),
        central_command="honeypot",
    ),
    ChannelCategory(
        "gif_detector_scope",
        "gif_detector_channels",
        "GIF detector scope",
        "scope",
        cardinality="multiple",
        thread_scope="parent",
        default_to_current_channel=True,
        required_permissions=("send_messages", "send_in_threads", "manage_messages"),
        central_command="gif-detector",
        module_command="gifdetector channel",
    ),
    ChannelCategory(
        "memes_source",
        "manual_evidence_memes_channel",
        "Memes source",
        "scope",
        allow_threads=False,
        central_command="memes",
        module_command="evidence memes_channel",
    ),
)

CATEGORIES_BY_KEY = {category.key: category for category in CHANNEL_CATEGORIES}


def category(key: str) -> ChannelCategory:
    try:
        return CATEGORIES_BY_KEY[key]
    except KeyError as error:
        raise ValueError(f"Unknown Honeypot channel category: {key}") from error


def _permission_arguments(spec: ChannelCategory) -> dict[str, bool]:
    arguments = dict.fromkeys(
        (
            "send_messages",
            "read_history",
            "manage_messages",
            "create_public_threads",
            "send_in_threads",
            "embed_links",
            "attach_files",
            "manage_threads",
        ),
        False,
    )
    arguments.update(dict.fromkeys(spec.required_permissions, True))
    return arguments


def _configuration_target(
    spec: ChannelCategory,
    target: discord.TextChannel | discord.Thread,
) -> tuple[int, discord.TextChannel | discord.Thread]:
    if spec.thread_scope == "parent":
        parent_id = getattr(target, "parent_id", None)
        if parent_id is not None:
            return parent_id, getattr(target, "parent", None) or target
    return target.id, target


def _validate_target(
    cog: Any,
    ctx: commands.Context,
    spec: ChannelCategory,
    target: discord.TextChannel | discord.Thread,
) -> None:
    if not spec.allow_threads and not isinstance(target, discord.TextChannel):
        raise commands.UserFeedbackCheckFailure(
            _("{label} must be a normal text channel.").format(label=spec.label)
        )
    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        raise commands.UserFeedbackCheckFailure(
            _("{label} must be a text channel or thread.").format(label=spec.label)
        )
    if spec.private and not cog._channel_is_private(ctx.guild, target):
        raise commands.UserFeedbackCheckFailure(
            _("{label} must be private.").format(label=spec.label)
        )
    missing = cog._missing_channel_permissions(
        ctx.guild,
        target,
        **_permission_arguments(spec),
    )
    if missing is not None:
        raise commands.UserFeedbackCheckFailure(missing)


async def configure_single(
    cog: Any,
    ctx: commands.Context,
    key: str,
    target: discord.TextChannel | discord.Thread | None = None,
) -> None:
    spec = category(key)
    if spec.cardinality != "single":
        raise ValueError(f"Channel category {key} is not single-valued")
    config = cog.config.guild(ctx.guild)
    accessor = getattr(config, spec.config_field)
    if target is None:
        channel_id = await accessor()
        await ctx.send(
            _("{label}: {channel}").format(
                label=spec.label,
                channel=cog._format_channel_setting(ctx.guild, channel_id),
            )
        )
        return
    _validate_target(cog, ctx, spec, target)
    await accessor.set(target.id)
    await ctx.send(
        _("✅ {label} channel set to {channel.mention}").format(
            label=spec.label,
            channel=target,
        )
    )


async def list_multiple(cog: Any, ctx: commands.Context, key: str) -> None:
    spec = category(key)
    if spec.cardinality != "multiple":
        raise ValueError(f"Channel category {key} is not multi-valued")
    accessor = getattr(cog.config.guild(ctx.guild), spec.config_field)
    async with accessor() as configured_ids:
        channel_ids = list(configured_ids)
    rendered = ", ".join(
        cog._format_channel_setting(ctx.guild, channel_id)
        for channel_id in channel_ids
    ) or _("Not configured")
    await ctx.send(_("{label}: {channels}").format(label=spec.label, channels=rendered))


async def add_multiple(
    cog: Any,
    ctx: commands.Context,
    key: str,
    target: discord.TextChannel | discord.Thread | None = None,
) -> None:
    spec = category(key)
    if spec.cardinality != "multiple":
        raise ValueError(f"Channel category {key} is not multi-valued")
    target = target or ctx.channel
    channel_id, scope_channel = _configuration_target(spec, target)
    _validate_target(cog, ctx, spec, scope_channel)
    accessor = getattr(cog.config.guild(ctx.guild), spec.config_field)
    async with accessor() as channel_ids:
        if channel_id in channel_ids:
            raise commands.UserFeedbackCheckFailure(
                _("{label} already includes that channel.").format(label=spec.label)
            )
        channel_ids.append(channel_id)
    await ctx.send(
        _("✅ {label} channel added: {channel.mention}").format(
            label=spec.label,
            channel=scope_channel,
        )
    )


async def remove_multiple(
    cog: Any,
    ctx: commands.Context,
    key: str,
    target: discord.TextChannel | discord.Thread | None = None,
) -> None:
    spec = category(key)
    if spec.cardinality != "multiple":
        raise ValueError(f"Channel category {key} is not multi-valued")
    target = target or ctx.channel
    channel_id, scope_channel = _configuration_target(spec, target)
    accessor = getattr(cog.config.guild(ctx.guild), spec.config_field)
    async with accessor() as channel_ids:
        if channel_id not in channel_ids:
            raise commands.UserFeedbackCheckFailure(
                _("{label} does not include that channel.").format(label=spec.label)
            )
        channel_ids.remove(channel_id)
    await ctx.send(
        _("✅ {label} channel removed: {channel.mention}").format(
            label=spec.label,
            channel=scope_channel,
        )
    )


async def clear_deleted_channel(cog: Any, channel: Any) -> None:
    guild_config = cog.config.guild(channel.guild)
    for spec in CHANNEL_CATEGORIES:
        accessor = getattr(guild_config, spec.config_field)
        if spec.cardinality == "single":
            if await accessor() == channel.id:
                await accessor.set(None)
            continue
        async with accessor() as channel_ids:
            while channel.id in channel_ids:
                channel_ids.remove(channel.id)


async def send_overview(cog: Any, ctx: commands.Context) -> None:
    configured = GuildSettings.from_mapping(await cog.config.guild(ctx.guild).all())
    entries = []
    for section, heading in (
        ("destination", _("Destinations")),
        ("scope", _("Sources and scopes")),
    ):
        rows = []
        for spec in CHANNEL_CATEGORIES:
            if spec.kind != section:
                continue
            value = getattr(configured, spec.config_field)
            if spec.cardinality == "single":
                rendered = cog._format_channel_setting(ctx.guild, value)
            else:
                rendered = ", ".join(
                    cog._format_channel_setting(ctx.guild, channel_id)
                    for channel_id in value
                ) or _("Not configured")
            rows.append(f"{spec.label}: {rendered}")
        if section == "destination":
            rows.append(
                _("GIF debug logging: {value}").format(
                    value=str(configured.gif_detector_debug_enabled).lower()
                )
            )
        entries.append((heading, "\n".join(rows)))
    prefix = getattr(ctx, "clean_prefix", "!")
    command_lines = []
    for spec in CHANNEL_CATEGORIES:
        if spec.central_command is None:
            continue
        base = f"{prefix}honeypot channels {spec.central_command}"
        if spec.cardinality == "single":
            command_lines.append(f"{base} [channel]")
            continue
        target = "[channel]" if spec.default_to_current_channel else "<channel>"
        command_lines.extend(
            (
                f"{base} add {target}",
                f"{base} remove {target}",
                f"{base} list",
            )
        )
        if spec.key == "honeypot_scope":
            command_lines.append(f"{base} create")
    entries.append((_("Commands"), "\n".join(command_lines)))
    await cog._send_config_dump(ctx, _("Honeypot channels"), entries)
