from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as datetime_time

import discord
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .activity_storage import (
    ActivityConsistencyReport,
    ActivityDatabaseStats,
    ActivityLocation,
    ActivityStore,
    ChannelTimelineDay,
    ChannelUserCount,
    DailyDominantLocation,
    DailySummary,
    TimelineDay,
    TopChannel,
    UserChannelDistribution,
    UserStats,
)
from .forum_autopin import ForumAutopinService
from .role_analytics_service import (
    FullMemberRequestCooldownError,
    MemberIntentRequiredError,
    RoleAnalyticsService,
    SyncAlreadyRunningError,
)
from .role_analytics_store import (
    AnalyticsUnavailableError,
    RoleAnalyticsStore,
    SyncStatus,
)
from .role_export import ExportMember, ExportTooLarge, build_role_export
from .role_expression import (
    RoleExpressionSyntaxError,
    compile_role_expression,
    parse_role_expression,
    render_role_expression,
    role_ids,
)
from .sticky_roles import StickyRoleStore
from .voice_activity import VoiceChannelVisitTracker

log = logging.getLogger("red.NHMisc")

DEFAULT_VCJUMPING_VISIT_COUNT = 3
DEFAULT_VCJUMPING_WINDOW_SECONDS = 30
DEFAULT_ACTIVITY_DETAIL_RETENTION_DAYS = 31
DEFAULT_ACTIVITY_HISTORY_RETENTION_DAYS = -1
CLEANUP_RESPONSE_TTL_SECONDS = 10
RETENTION_CONFIRMATION = "I understand"

# Categorical chart hues in fixed rank order, arranged so neighboring bars use
# clearly different colors. The neutral tone is reserved for undisplayed users.
CHATCHART_SERIES_COLORS = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
    "#00a6d6",
    "#7a5c00",
    "#a1c935",
    "#9f55d4",
    "#c44e9b",
    "#006d77",
    "#f48c06",
    "#264653",
    "#9b5de5",
    "#ef476f",
    "#118ab2",
    "#6a994e",
)
CHATCHART_OTHER_COLOR = "#898781"
DEFAULT_CHATCHART_USER_COUNT = 10
MAX_CHATCHART_USER_COUNT = 20
DISCORD_SNOWFLAKE_MIN_DIGITS = 15
STARGATE_EMOJI_NAME = "stargate"
STARGATE_EMOJI_ID = 769315278953381928
GATE_TIER_ROLE_IDS = (
    798700443979087892,
    1004822424921055233,
    1097204292198338692,
    1442209801374269682,
    1437811360208781406,
    1522017144878137385,
)
SINGLEPLAYER_GATE_COMPLETED_ROLE_ID = 1442208051212976158
TIER_DISTRIBUTION_ROLES = (
    ("Stone", "stoneTier", 757571320945967205, 757645112267243541),
    ("Steam", "steamTier", 757571510880829540, 757643319265460224),
    ("LV", "lvTier", 757571726790885378, 630848584539045926),
    ("MV", "mvTier", 757571761159012383, 631180331839389738),
    ("HV", "hvTier", 757571801961201714, 631180321727184896),
    ("EV", "evTier", 757571842209873991, 631180312906563594),
    ("IV", "ivTier", 757571883268046908, 631180295252738099),
    ("LuV", "luvTier", 757571961114066994, 631180266982866986),
    ("ZPM", "zpmTier", 757571992500305962, 631180246837624852),
    ("UV", "uvTier", 757572023269720078, 631180223928336414),
    ("UHV", "uhvTier", 757572062058643467, 631180193960296478),
    ("UEV", "uevTier", 888133083931476009, 631180158262575174),
    ("UIV", "uivTier", 888133292547772467, 631180143385247754),
    ("UMV", "umvTier", 888133377620852776, 631180120782012426),
    ("UXV", "uxvTier", 888133463461494864, 631180089782042625),
)


def _require_guild_role(
    guild,
    role_id: int,
    *,
    report_name: str,
    role_label: str,
):
    role = guild.get_role(role_id)
    if role is None:
        raise commands.UserFeedbackCheckFailure(
            f"{report_name} is misconfigured: {role_label} role "
            f"({role_id}) was not found in this server."
        )
    return role


class NHMisc(commands.Cog):
    """Miscellaneous small utilities for Red-DiscordBot."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=8597423150612235807,
            force_registration=True,
        )
        self.config.register_guild(
            voice_log_channel=None,
            alert_channel=None,
            vcjumping_visit_count=DEFAULT_VCJUMPING_VISIT_COUNT,
            vcjumping_window_seconds=DEFAULT_VCJUMPING_WINDOW_SECONDS,
            activity_channel=None,
            activity_detail_retention_days=DEFAULT_ACTIVITY_DETAIL_RETENTION_DAYS,
            activity_history_retention_days=DEFAULT_ACTIVITY_HISTORY_RETENTION_DAYS,
            sticky_debug_logging_enabled=False,
            sticky_debug_logging_channel=None,
            forum_autopin_channel_ids=[],
        )
        self._voice_visits = VoiceChannelVisitTracker()
        self._audit_log_tasks: set[asyncio.Task] = set()
        self._forum_autopin = ForumAutopinService(
            self.config, alert_sender=self._send_guild_alert, logger=log
        )
        self._activity_store = ActivityStore(cog_data_path(self) / "activity.sqlite")
        self._sticky_roles = StickyRoleStore(cog_data_path(self) / "sticky_roles.sqlite")
        self._role_analytics_store = RoleAnalyticsStore(
            cog_data_path(self) / "role_analytics.sqlite"
        )
        self._role_analytics = RoleAnalyticsService(
            self.bot, self._role_analytics_store, logger=log
        )
        self._activity_task: asyncio.Task | None = None
        self._role_analytics_startup_task: asyncio.Task | None = None
        self._role_analytics_daily_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        await self._activity_store.initialize()
        await self._sticky_roles.initialize()
        await self._role_analytics_store.initialize()
        self._activity_task = asyncio.create_task(self._activity_midnight_loop())
        self._role_analytics_startup_task = asyncio.create_task(
            self._role_analytics_startup_reconcile()
        )
        self._role_analytics_daily_task = asyncio.create_task(
            self._role_analytics_daily_loop()
        )

    def cog_unload(self) -> None:
        for task in self._audit_log_tasks:
            task.cancel()
        if self._activity_task is not None:
            self._activity_task.cancel()
        if self._role_analytics_startup_task is not None:
            self._role_analytics_startup_task.cancel()
        if self._role_analytics_daily_task is not None:
            self._role_analytics_daily_task.cancel()
        self._role_analytics.cancel()

    async def configured_sticky_role_ids(self, guild_id: int) -> frozenset[int]:
        return frozenset(await self._sticky_roles.get_sticky_roles(guild_id))

    async def _role_analytics_startup_reconcile(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self._role_analytics.reconcile_enabled_guilds(tuple(self.bot.guilds))
        except Exception:
            log.exception("Failed to reconcile role analytics on startup")

    async def _role_analytics_daily_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                await self._role_analytics.run_daily_reconciliation(
                    tuple(self.bot.guilds)
                )
            except Exception:
                log.exception("Failed to run daily role analytics reconciliation")

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        await self._role_analytics_store.delete_user_everywhere(user_id)

    @commands.command(name="rolesync")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def rolesync(self, ctx: commands.Context) -> None:
        """Initialize or reconcile the role analytics database."""
        if self._role_analytics.is_syncing(ctx.guild.id):
            raise commands.UserFeedbackCheckFailure(
                "Role synchronization is already running"
            )
        await ctx.send("Role synchronization started")
        try:
            result = await self._role_analytics.sync_guild(ctx.guild, manual=True)
        except SyncAlreadyRunningError as error:
            raise commands.UserFeedbackCheckFailure(
                "Role synchronization is already running"
            ) from error
        except (MemberIntentRequiredError, FullMemberRequestCooldownError) as error:
            log.warning("Role synchronization unavailable for guild %s: %s", ctx.guild.id, error)
            raise commands.UserFeedbackCheckFailure(
                "Role synchronization is unavailable right now"
            ) from error
        except Exception as error:
            log.exception("Role synchronization failed for guild %s", ctx.guild.id)
            raise commands.UserFeedbackCheckFailure(
                "Role synchronization failed"
            ) from error

        await ctx.send(
            f"Role synchronization complete: {result.member_count} members, "
            f"{result.membership_count} role memberships in {result.elapsed_seconds:.1f}s"
        )

    @commands.command(name="rolestats")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def rolestats(self, ctx: commands.Context, *, expression: str) -> None:
        """Count members matching a boolean role expression."""
        parsed, predicate_sql, parameters = self._prepare_role_expression(
            ctx.guild, expression
        )
        try:
            count = await self._role_analytics_store.count_matching(
                ctx.guild.id, predicate_sql, parameters
            )
        except AnalyticsUnavailableError as error:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            ) from error

        await ctx.send(
            f"{count} users match: {render_role_expression(parsed)}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="roleusers")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    async def roleusers(self, ctx: commands.Context, *, expression: str) -> None:
        """Export members matching a boolean role expression."""
        self._require_private_role_export_channel(ctx)
        parsed, predicate_sql, parameters = self._prepare_role_expression(
            ctx.guild, expression
        )
        if not bool(ctx.guild.chunked):
            await self._repair_role_analytics_cache(ctx.guild)
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            )

        try:
            user_ids = await self._role_analytics_store.matching_user_ids(
                ctx.guild.id, predicate_sql, parameters
            )
        except AnalyticsUnavailableError as error:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            ) from error
        if not user_ids:
            await ctx.send("No users match this expression")
            return

        members = [ctx.guild.get_member(user_id) for user_id in user_ids]
        if any(member is None for member in members):
            await self._repair_role_analytics_cache(ctx.guild)
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            )

        export_members = tuple(
            ExportMember(member.id, member.name, member.display_name)
            for member in members
        )
        try:
            payload = build_role_export(export_members, ctx.guild.filesize_limit)
        except ExportTooLarge:
            await ctx.send("Export is too large to upload")
            return

        await ctx.send(
            f"{len(user_ids)} users match: {render_role_expression(parsed)}",
            file=discord.File(io.BytesIO(payload.data), filename=payload.filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _configuration_embed(
        self,
        *,
        ctx: commands.Context,
        title: str,
        current: tuple[str, ...],
        action_heading: str = "Commands",
    ) -> discord.Embed:
        embed = discord.Embed(title=title)
        current_value = (
            "Run this command in a channel hidden from @everyone "
            "to view the current configuration."
            if self._channel_is_public(ctx)
            else "\n".join(current)
        )
        embed.add_field(
            name="Current configuration",
            value=current_value,
            inline=False,
        )
        embed.add_field(
            name=action_heading,
            value=self._format_direct_commands(ctx),
            inline=False,
        )
        return embed

    @staticmethod
    def _channel_is_public(ctx: commands.Context) -> bool:
        permissions = ctx.channel.permissions_for(ctx.guild.default_role)
        return bool(permissions.view_channel)

    @staticmethod
    def _format_direct_commands(ctx: commands.Context) -> str:
        lines = []
        for command in ctx.command.commands:
            if command.hidden:
                continue
            signature = command.signature.strip()
            usage = f"{ctx.clean_prefix}{command.qualified_name}"
            if signature:
                usage = f"{usage} {signature}"
            lines.append(f"`{usage}`")
        return "\n".join(lines) or "No subcommands available."

    @staticmethod
    def _configured_channel_label(
        guild: discord.Guild, channel_id: int | None
    ) -> str:
        if channel_id is None:
            return "Not configured"
        channel = guild.get_channel(channel_id)
        if channel is None:
            return "Configured channel is missing"
        return channel.mention

    @commands.group(name="nhmisc", invoke_without_command=True)
    @commands.guild_only()
    async def nhmisc(self, ctx: commands.Context) -> None:
        """Configure NHMisc."""
        embed = discord.Embed(
            title="NHMisc",
            description="Configuration, activity, and moderation tools.",
        )
        embed.add_field(
            name="Commands",
            value=self._format_direct_commands(ctx),
            inline=False,
        )
        await ctx.send(embed=embed)

    def _loaded_honeypot(self):
        honeypot = self.bot.get_cog("Honeypot")
        if honeypot is None:
            return None
        if not callable(getattr(honeypot, "cleanup_channel", None)):
            return None
        if not callable(getattr(honeypot, "cleanup_user", None)):
            return None
        return honeypot

    @nhmisc.group(name="cleanup", invoke_without_command=True)
    @commands.mod_or_permissions(manage_messages=True)
    async def nhmisc_cleanup(self, ctx: commands.Context, count: int) -> None:
        """Delete recently observed messages from the current channel."""
        if not 1 <= count <= 100:
            raise commands.UserFeedbackCheckFailure(
                "Count must be between 1 and 100."
            )
        honeypot = self._loaded_honeypot()
        if honeypot is None:
            await ctx.send("Honeypot is not loaded, so cleanup is unavailable.")
            return
        try:
            result = await honeypot.cleanup_channel(ctx, count)
        except Exception:
            log.exception("Honeypot channel cleanup failed")
            await ctx.send("Cleanup failed. Check the bot logs and try again.")
            return
        await ctx.send(
            result.public_message,
            delete_after=CLEANUP_RESPONSE_TTL_SECONDS,
        )

    @nhmisc_cleanup.command(name="user")
    @commands.mod_or_permissions(manage_messages=True)
    async def nhmisc_cleanup_user(
        self,
        ctx: commands.Context,
        target: str,
        count: int,
    ) -> None:
        """Delete recently observed messages from a user across this server."""
        if not 1 <= count <= 100:
            raise commands.UserFeedbackCheckFailure(
                "Count must be between 1 and 100."
            )
        user_id = self._parse_user_id(target)
        honeypot = self._loaded_honeypot()
        if honeypot is None:
            await ctx.send("Honeypot is not loaded, so cleanup is unavailable.")
            return
        try:
            result = await honeypot.cleanup_user(ctx, user_id, count)
        except Exception:
            log.exception("Honeypot user cleanup failed")
            await ctx.send("Cleanup failed. Check the bot logs and try again.")
            return
        await ctx.send(
            result.public_message,
            delete_after=CLEANUP_RESPONSE_TTL_SECONDS,
        )

    @nhmisc.group(name="roleanalytics", invoke_without_command=True)
    @commands.admin_or_permissions(administrator=True)
    async def nhmisc_roleanalytics(self, ctx: commands.Context) -> None:
        """Configure role analytics."""
        state = await self._role_analytics_store.get_state(ctx.guild.id)
        member_count = (
            f"{state.source_member_count:,}"
            if state.source_member_count is not None
            else "Not available"
        )
        embed = self._configuration_embed(
            ctx=ctx,
            title="Role analytics",
            current=(
                f"Enabled: {'Yes' if state.enabled else 'No'}",
                f"Status: {state.status.value.replace('_', ' ').title()}",
                f"Members in snapshot: {member_count}",
            ),
        )
        await ctx.send(embed=embed)

    @nhmisc_roleanalytics.command(name="disable")
    async def nhmisc_roleanalytics_disable(self, ctx: commands.Context) -> None:
        """Disable role analytics and delete this guild's analytics database."""
        await self._role_analytics.disable_guild(ctx.guild.id)
        await ctx.send("Role analytics disabled")

    @nhmisc.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the text channel used for voice event logs."""
        missing_permissions = self._missing_log_permissions(ctx.guild, channel)
        if missing_permissions is not None:
            raise commands.UserFeedbackCheckFailure(missing_permissions)

        await self.config.guild(ctx.guild).voice_log_channel.set(channel.id)
        await ctx.send(f"Voice log channel set to {channel.mention}.")

    @nhmisc.group(name="alert", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_alert(self, ctx: commands.Context) -> None:
        """Configure alert logging."""
        channel_id = await self.config.guild(ctx.guild).alert_channel()
        embed = self._configuration_embed(
            ctx=ctx,
            title="Alert logging",
            current=(
                f"Channel: {self._configured_channel_label(ctx.guild, channel_id)}",
            ),
            action_heading="Change it",
        )
        await ctx.send(embed=embed)

    @nhmisc_alert.command(name="channel")
    async def nhmisc_alert_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the text channel used for alert logs."""
        missing_permissions = self._missing_log_permissions(ctx.guild, channel)
        if missing_permissions is not None:
            raise commands.UserFeedbackCheckFailure(missing_permissions)

        await self.config.guild(ctx.guild).alert_channel.set(channel.id)
        await ctx.send(f"Alert channel set to {channel.mention}.")

    @nhmisc.group(name="vcjumping", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_vcjumping(self, ctx: commands.Context) -> None:
        """Configure voice channel jumping detection."""
        config = await self.config.guild(ctx.guild).all()
        embed = self._configuration_embed(
            ctx=ctx,
            title="VC jumping detection",
            current=(
                f"Channel entries: {config['vcjumping_visit_count']}",
                f"Time window: {config['vcjumping_window_seconds']} seconds",
            ),
            action_heading="Change it",
        )
        await ctx.send(embed=embed)

    @nhmisc_vcjumping.command(name="visits")
    async def nhmisc_vcjumping_visits(self, ctx: commands.Context, count: int) -> None:
        """Set how many voice channel entries trigger VC jumping alerts."""
        if count < 2:
            raise commands.UserFeedbackCheckFailure("VC jumping visit count must be at least 2.")

        await self.config.guild(ctx.guild).vcjumping_visit_count.set(count)
        await ctx.send(f"VC jumping alerts will trigger after {count} channel entries.")

    @nhmisc_vcjumping.command(name="seconds")
    async def nhmisc_vcjumping_seconds(self, ctx: commands.Context, seconds: int) -> None:
        """Set the VC jumping detection time window in seconds."""
        if seconds < 1:
            raise commands.UserFeedbackCheckFailure("VC jumping window must be at least 1 second.")

        await self.config.guild(ctx.guild).vcjumping_window_seconds.set(seconds)
        await ctx.send(f"VC jumping window set to {seconds} seconds.")

    @nhmisc.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_status(self, ctx: commands.Context) -> None:
        """Show the current voice log configuration."""
        config = await self.config.guild(ctx.guild).all()
        channel = self._get_log_channel(ctx.guild, config["voice_log_channel"])
        alert_channel = self._get_log_channel(ctx.guild, config["alert_channel"])
        channel_label = channel.mention if channel is not None else "not set"
        alert_channel_label = alert_channel.mention if alert_channel is not None else "not set"
        await ctx.send(
            "Voice log channel: {channel}\n"
            "Alert channel: {alert_channel}\n"
            "VC jumping: {count} channel entries in {seconds} seconds.".format(
                channel=channel_label,
                alert_channel=alert_channel_label,
                count=config["vcjumping_visit_count"],
                seconds=config["vcjumping_window_seconds"],
            )
        )

    @nhmisc.group(name="forumautopin", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_forumautopin(self, ctx: commands.Context) -> None:
        """Configure automatic pinning for new forum post starter messages."""
        configured = await self._forum_autopin.get_forum_ids(ctx.guild)
        forum_lines = [f"Configured forums: {len(configured)}"]
        for channel_id in configured[:10]:
            channel = ctx.guild.get_channel(channel_id)
            forum_lines.append(
                channel.mention if channel is not None else "Configured forum is missing"
            )
        if len(configured) > 10:
            forum_lines.append(f"...and {len(configured) - 10} more")

        embed = self._configuration_embed(
            ctx=ctx,
            title="Forum autopin",
            current=tuple(forum_lines),
        )
        await ctx.send(embed=embed)

    @nhmisc_forumautopin.command(name="add")
    async def nhmisc_forumautopin_add(
        self,
        ctx: commands.Context,
        channel: discord.ForumChannel,
    ) -> None:
        """Enable starter-message autopinning in a forum."""
        missing_permission = self._forum_autopin.missing_permissions(ctx.guild, channel)
        if missing_permission is not None:
            raise commands.UserFeedbackCheckFailure(missing_permission)

        enabled = await self._forum_autopin.enable(ctx.guild, channel.id)
        state = "is now enabled" if enabled else "is already enabled"
        await ctx.send(
            f"Forum autopin {state} for {channel.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmisc_forumautopin.command(name="remove")
    async def nhmisc_forumautopin_remove(
        self,
        ctx: commands.Context,
        channel: discord.ForumChannel,
    ) -> None:
        """Disable starter-message autopinning in a forum."""
        disabled = await self._forum_autopin.disable(ctx.guild, channel.id)
        state = "is disabled" if disabled else "is not enabled"
        await ctx.send(
            f"Forum autopin {state} for {channel.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmisc_forumautopin.command(name="list")
    async def nhmisc_forumautopin_list(self, ctx: commands.Context) -> None:
        """List forums configured for starter-message autopinning."""
        configured = await self._forum_autopin.get_forum_ids(ctx.guild)
        if not configured:
            await ctx.send(
                "No forums are configured for automatic starter-message pinning."
            )
            return

        lines = ["Forums with starter-message autopinning:"]
        for channel_id in configured:
            channel = ctx.guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
            if isinstance(channel, discord.ForumChannel):
                lines.append(f"- {channel.mention} (`{channel_id}`)")
            else:
                lines.append(f"- Missing forum (`{channel_id}`)")
        await self._send_paginated_text(ctx, "\n".join(lines))

    @commands.command(name="gatecount")
    @commands.guild_only()
    async def gatecount(self, ctx: commands.Context) -> None:
        """Show member counts for the current Gate roles."""
        _require_guild_role(
            ctx.guild,
            SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            report_name="Gatecount",
            role_label="Singleplayer completed",
        )
        for tier, role_id in enumerate(GATE_TIER_ROLE_IDS, start=1):
            _require_guild_role(
                ctx.guild,
                role_id,
                report_name="Gatecount",
                role_label=f"Tier {tier}",
            )
        singleplayer_count = await self._count_role_expression(
            ctx.guild, str(SINGLEPLAYER_GATE_COMPLETED_ROLE_ID)
        )
        tier_counts = await self._count_highest_role_buckets(
            ctx.guild, GATE_TIER_ROLE_IDS
        )
        total_gates = sum(
            tier * count for tier, count in enumerate(tier_counts, start=1)
        )

        def format_role_count(role_id: int, count: int) -> str:
            player_label = "player" if count == 1 else "players"
            return f"<@&{role_id}> — **{count} {player_label}**"

        lines = [
            format_role_count(
                SINGLEPLAYER_GATE_COMPLETED_ROLE_ID, singleplayer_count
            )
        ]
        lines.extend(
            format_role_count(role_id, count)
            for role_id, count in zip(GATE_TIER_ROLE_IDS, tier_counts, strict=True)
        )
        lines.extend(("", f"**Total Gates: {total_gates}**"))
        embed = discord.Embed(
            title="Current Gatecount:",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="tierdistribution")
    @commands.guild_only()
    async def tierdistribution(self, ctx: commands.Context) -> None:
        """Show the current distribution of progression and Gate player roles."""
        resolved_tiers = []
        for label, emoji_name, emoji_id, role_id in TIER_DISTRIBUTION_ROLES:
            role = _require_guild_role(
                ctx.guild,
                role_id,
                report_name="Tier distribution",
                role_label=label,
            )
            resolved_tiers.append((emoji_name, emoji_id, role))

        resolved_gate_roles = [
            _require_guild_role(
                ctx.guild,
                role_id,
                report_name="Tier distribution",
                role_label=f"Gate Tier {tier}",
            )
            for tier, role_id in enumerate(GATE_TIER_ROLE_IDS, start=1)
        ]

        tier_bucket_counts = await self._count_highest_role_buckets(
            ctx.guild,
            tuple(role.id for _, _, role in resolved_tiers),
        )
        tier_counts = [
            (emoji_name, emoji_id, count)
            for (emoji_name, emoji_id, _), count in zip(
                resolved_tiers, tier_bucket_counts, strict=True
            )
        ]
        gate_count = await self._count_role_expression(
            ctx.guild,
            " OR ".join(str(role.id) for role in resolved_gate_roles),
        )
        total_count = sum(count for _, _, count in tier_counts) + gate_count

        def format_count(count: int) -> str:
            player_label = "Player" if count == 1 else "Players"
            percentage = count / total_count * 100 if total_count else 0.0
            return f"**{count} {player_label}** ({percentage:.1f}%)"

        lines = [
            f"<:{emoji_name}:{emoji_id}> — {format_count(count)}"
            for emoji_name, emoji_id, count in tier_counts
        ]
        lines.append(
            f"<:{STARGATE_EMOJI_NAME}:{STARGATE_EMOJI_ID}> — "
            f"{format_count(gate_count)}"
        )

        embed = discord.Embed(
            title="Current Tier Distribution:",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await ctx.send(embed=embed)

    @nhmisc.group(name="stickyroles", invoke_without_command=True)
    async def nhmisc_stickyroles(self, ctx: commands.Context) -> None:
        """Configure sticky role persistence."""
        await self._require_manage_guild(ctx)
        role_ids = sorted(await self._sticky_roles.get_sticky_roles(ctx.guild.id))
        role_lines = [f"Configured roles: {len(role_ids)}"]
        for role_id in role_ids[:10]:
            role = ctx.guild.get_role(role_id)
            role_lines.append(
                role.mention if role is not None else "Configured role is missing"
            )
        if len(role_ids) > 10:
            role_lines.append(f"...and {len(role_ids) - 10} more")

        embed = self._configuration_embed(
            ctx=ctx,
            title="Sticky roles",
            current=tuple(role_lines),
        )
        await ctx.send(embed=embed)

    @nhmisc_stickyroles.command(name="add")
    async def nhmisc_stickyroles_add(self, ctx: commands.Context, role: str) -> None:
        """Mark a role as sticky by role mention or raw role ID."""
        await self._require_manage_guild(ctx)
        role_id = self._parse_role_id(role)
        discord_role = ctx.guild.get_role(role_id)
        if discord_role is None:
            raise commands.UserFeedbackCheckFailure("That role does not exist on this server.")
        if not self._can_restore_role(ctx.guild, discord_role):
            raise commands.UserFeedbackCheckFailure(
                "I cannot restore that role. Check Manage Roles and role hierarchy."
            )

        added = await self._sticky_roles.add_sticky_role(ctx.guild.id, role_id)
        if added:
            await ctx.send(
                f"{discord_role.mention} is now sticky.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                f"{discord_role.mention} is already sticky.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @nhmisc_stickyroles.command(name="remove")
    async def nhmisc_stickyroles_remove(self, ctx: commands.Context, role: str) -> None:
        """Remove a sticky role by role mention or raw role ID."""
        await self._require_manage_guild(ctx)
        role_id = self._parse_role_id(role)
        config_exists, saved_rows = await self._sticky_roles.get_role_state(
            ctx.guild.id, role_id
        )
        if not config_exists and saved_rows == 0:
            await ctx.send(
                f"{self._format_role_reference(ctx.guild, role_id)} is not present in the sticky role DB.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self._prompt_sticky_role_db_action(
            guild=ctx.guild,
            channel=ctx.channel,
            role_id=role_id,
            role_name=self._role_name_for_prompt(ctx.guild, role_id),
            config_exists=config_exists,
            saved_rows=saved_rows,
            reason="manual remove command",
            requester=ctx.author,
        )

    @nhmisc_stickyroles.command(name="list")
    async def nhmisc_stickyroles_list(self, ctx: commands.Context) -> None:
        """List sticky roles configured for this server."""
        await self._require_manage_guild(ctx)
        role_ids = await self._sticky_roles.get_sticky_roles(ctx.guild.id)
        if not role_ids:
            await ctx.send("No sticky roles are configured on this server.")
            return

        lines = ["Sticky roles:"]
        for role_id in sorted(role_ids):
            lines.append(f"- {self._format_role_reference(ctx.guild, role_id)}")
        await self._send_paginated_text(ctx, "\n".join(lines))

    @nhmisc_stickyroles.command(name="scan")
    async def nhmisc_stickyroles_scan(self, ctx: commands.Context) -> None:
        """Scan sticky role DB for role IDs missing from Discord."""
        await self._require_manage_guild(ctx)
        existing_role_ids = {role.id for role in ctx.guild.roles}
        orphaned_roles = await self._sticky_roles.get_orphaned_roles(
            ctx.guild.id, existing_role_ids
        )
        if not orphaned_roles:
            await ctx.send("No sticky role DB entries need review.")
            return

        await ctx.send(
            f"Found {len(orphaned_roles)} sticky role DB entries that need review. "
            "I will ask about them one by one."
        )
        for role_id, config_exists, saved_rows in orphaned_roles:
            await self._prompt_sticky_role_db_action(
                guild=ctx.guild,
                channel=ctx.channel,
                role_id=role_id,
                role_name=None,
                config_exists=config_exists,
                saved_rows=saved_rows,
                reason="manual orphan scan",
                requester=ctx.author,
            )

    @nhmisc_stickyroles.group(name="debuglogging", invoke_without_command=True)
    async def nhmisc_stickyroles_debuglogging(self, ctx: commands.Context) -> None:
        """Configure sticky role debug logging."""
        await self._require_manage_guild(ctx)
        config = await self.config.guild(ctx.guild).all()
        embed = self._configuration_embed(
            ctx=ctx,
            title="Sticky role debug logging",
            current=(
                "Enabled: "
                + ("Yes" if config["sticky_debug_logging_enabled"] else "No"),
                "Channel: "
                + self._configured_channel_label(
                    ctx.guild,
                    config["sticky_debug_logging_channel"]
                ),
            ),
            action_heading="Change it",
        )
        await ctx.send(embed=embed)

    @nhmisc_stickyroles_debuglogging.command(name="toggle")
    async def nhmisc_stickyroles_debuglogging_toggle(
        self, ctx: commands.Context, enabled: bool
    ) -> None:
        """Enable or disable sticky role debug logging."""
        await self._require_manage_guild(ctx)
        await self.config.guild(ctx.guild).sticky_debug_logging_enabled.set(enabled)
        state = "enabled" if enabled else "disabled"
        await ctx.send(f"Sticky role debug logging {state}.")

    @nhmisc_stickyroles_debuglogging.command(name="channel")
    async def nhmisc_stickyroles_debuglogging_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the sticky role debug logging channel."""
        await self._require_manage_guild(ctx)
        missing_permissions = self._missing_log_permissions(ctx.guild, channel)
        if missing_permissions is not None:
            raise commands.UserFeedbackCheckFailure(missing_permissions)

        await self.config.guild(ctx.guild).sticky_debug_logging_channel.set(channel.id)
        await ctx.send(f"Sticky role debug logging channel set to {channel.mention}.")

    @nhmisc.group(name="activity", invoke_without_command=True)
    async def nhmisc_activity(self, ctx: commands.Context) -> None:
        """Configure and inspect passive message activity summaries."""
        await self._require_activity_staff(ctx)
        config = await self.config.guild(ctx.guild).all()
        history_days = config["activity_history_retention_days"]
        if history_days < 0:
            history_label = "Unlimited"
        elif history_days == 0:
            history_label = "Disabled"
        else:
            history_label = f"{history_days} days"

        embed = self._configuration_embed(
            ctx=ctx,
            title="Activity tracking",
            current=(
                "Summary channel: "
                f"{self._configured_channel_label(ctx.guild, config['activity_channel'])}",
                f"Detail retention: {config['activity_detail_retention_days']} days",
                f"History retention: {history_label}",
            ),
        )
        await ctx.send(embed=embed)

    @nhmisc_activity.command(name="channel")
    async def nhmisc_activity_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the channel used for automatic daily activity summaries."""
        await self._require_manage_guild(ctx)
        missing_permissions = self._missing_log_permissions(ctx.guild, channel)
        if missing_permissions is not None:
            raise commands.UserFeedbackCheckFailure(missing_permissions)

        await self.config.guild(ctx.guild).activity_channel.set(channel.id)
        await ctx.send(f"Activity summary channel set to {channel.mention}.")

    @nhmisc_activity.command(name="current")
    async def nhmisc_activity_current(self, ctx: commands.Context) -> None:
        """Preview the current UTC day's activity without closing it."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        today = self._utc_today()
        summary = await self._activity_store.build_current_summary(
            ctx.guild.id, today, ctx.guild.member_count or 0
        )
        if summary is None:
            await ctx.send("No activity data has been collected for the current UTC day.")
            return

        await ctx.send(embed=self._build_daily_summary_embed(summary, title_prefix="Current day"))

    @nhmisc_activity.command(name="latest")
    async def nhmisc_activity_latest(self, ctx: commands.Context) -> None:
        """Repost the latest retained closed daily activity summary."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        summary = await self._activity_store.get_latest_summary(ctx.guild.id)
        if summary is None:
            await ctx.send("No retained daily activity summary is available.")
            return

        await ctx.send(embed=self._build_daily_summary_embed(summary, title_prefix="Latest day"))

    @nhmisc_activity.command(name="timeline")
    async def nhmisc_activity_timeline(self, ctx: commands.Context, days: int) -> None:
        """Show a compact timeline for retained closed daily summaries."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Days must be at least 1.")

        config = await self.config.guild(ctx.guild).all()
        history_retention = int(config["activity_history_retention_days"])
        if history_retention == 0:
            await ctx.send("Historical activity summaries are not retained on this server.")
            return
        if history_retention > 0 and days > history_retention:
            days = history_retention

        end_date = self._utc_today() - timedelta(days=1)
        timeline = await self._activity_store.get_timeline(ctx.guild.id, end_date, days)
        top_channels = await self._activity_store.get_timeline_top_channels(
            ctx.guild.id, end_date, days
        )
        await ctx.send(embed=self._build_timeline_embed(timeline, top_channels, days))

    @nhmisc_activity.command(name="channelstats")
    async def nhmisc_activity_channelstats(
        self, ctx: commands.Context, channel: discord.TextChannel, days: int
    ) -> None:
        """Show message activity for a channel day by day."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Days must be at least 1.")

        config = await self.config.guild(ctx.guild).all()
        history_retention = int(config["activity_history_retention_days"])
        detail_retention = max(1, int(config["activity_detail_retention_days"]))
        if history_retention == 0:
            days = min(days, detail_retention)
        elif history_retention > 0:
            days = min(days, max(history_retention, detail_retention))

        timeline = await self._activity_store.get_channel_timeline(
            ctx.guild.id,
            channel.id,
            None,
            self._utc_today(),
            days,
        )
        await ctx.send(embed=self._build_channel_timeline_embed(channel, timeline, days))

    @nhmisc_activity.command(name="verify")
    async def nhmisc_activity_verify(self, ctx: commands.Context) -> None:
        """Verify today's aggregate activity cache consistency."""
        await self._require_activity_staff(ctx)
        today = self._utc_today()
        report = await self._activity_store.verify_open_day_consistency(ctx.guild.id, today)
        await ctx.send(embed=self._build_activity_consistency_embed(report, today))

    @nhmisc_activity.command(name="dbsize")
    async def nhmisc_activity_dbsize(self, ctx: commands.Context) -> None:
        """Show activity SQLite database size and row counts."""
        await self._require_activity_staff(ctx)
        stats = await self._activity_store.get_database_stats()
        await ctx.send(embed=self._build_activity_database_stats_embed(stats))

    @nhmisc_activity.command(name="retention")
    async def nhmisc_activity_retention(self, ctx: commands.Context, days: int) -> None:
        """Set how many days of per-user/channel detail rows are retained."""
        await self._require_manage_guild(ctx)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Detail retention must be at least 1 day.")

        cutoff = self._utc_today() - timedelta(days=days - 1)
        rows_to_delete = await self._activity_store.count_detail_rows_older_than(
            ctx.guild.id, cutoff
        )
        if rows_to_delete:
            confirmed = await self._confirm_retention_delete(
                ctx,
                (
                    f"Changing detail retention to {days} days will permanently delete "
                    f"{rows_to_delete} user/channel detail rows older than {cutoff.isoformat()}.\n"
                    f"Reply with `{RETENTION_CONFIRMATION}` to continue."
                ),
            )
            if not confirmed:
                return
            deleted = await self._activity_store.prune_detail_rows_older_than(ctx.guild.id, cutoff)
            await ctx.send(f"Deleted {deleted} detail rows.")

        await self.config.guild(ctx.guild).activity_detail_retention_days.set(days)
        await ctx.send(f"Activity detail retention set to {days} days.")

    @nhmisc_activity.command(name="historyretention")
    async def nhmisc_activity_history_retention(self, ctx: commands.Context, days: int) -> None:
        """Set how many closed daily aggregate summaries are retained."""
        await self._require_manage_guild(ctx)
        if days < -1:
            raise commands.UserFeedbackCheckFailure(
                "History retention must be -1, 0, or a positive number of days."
            )

        cutoff = self._history_retention_cutoff(days)
        summary_rows = top_rows = channel_rows = 0
        if cutoff is not None:
            (
                summary_rows,
                top_rows,
                channel_rows,
            ) = await self._activity_store.count_history_rows_older_than(ctx.guild.id, cutoff)
        if summary_rows or top_rows or channel_rows:
            confirmed = await self._confirm_retention_delete(
                ctx,
                (
                    f"Changing history retention to {days} will permanently delete "
                    f"{summary_rows} daily summary rows, {top_rows} top-channel rows, "
                    f"and {channel_rows} channel summary rows "
                    f"older than {cutoff.isoformat()}.\n"
                    f"Reply with `{RETENTION_CONFIRMATION}` to continue."
                ),
            )
            if not confirmed:
                return
            (
                deleted_summary,
                deleted_top,
                deleted_channel,
            ) = await self._activity_store.prune_history_rows_older_than(ctx.guild.id, cutoff)
            await ctx.send(
                f"Deleted {deleted_summary} daily summary rows, {deleted_top} top-channel rows, "
                f"and {deleted_channel} channel summary rows."
            )

        await self.config.guild(ctx.guild).activity_history_retention_days.set(days)
        await ctx.send(f"Activity history retention set to {days}.")

    @nhmisc.group(name="usermodstats", invoke_without_command=True)
    async def nhmisc_usermodstats(
        self, ctx: commands.Context, target: str, range_text: str
    ) -> None:
        """Show moderator-only message activity stats for a user."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        user_id = self._parse_user_id(target)
        days = self._parse_range_days(range_text)
        days = await self._cap_detail_days(ctx.guild, days)
        end_date = self._utc_today()
        stats = await self._activity_store.get_user_stats(ctx.guild.id, user_id, end_date, days)

        title = f"User activity: {self._format_user_reference(ctx.guild, user_id)}"
        await ctx.send(embed=self._build_user_stats_embed(title, stats, days))

    @nhmisc_usermodstats.command(name="channel")
    async def nhmisc_usermodstats_channel(
        self,
        ctx: commands.Context,
        target: str,
        channel_text: str,
        range_text: str,
    ) -> None:
        """Show moderator-only message activity stats for a user in one channel."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        user_id = self._parse_user_id(target)
        days = self._parse_range_days(range_text)
        days = await self._cap_detail_days(ctx.guild, days)
        end_date = self._utc_today()
        channel = self._resolve_text_channel_or_thread(ctx.guild, channel_text)
        parent_channel_id = self._activity_parent_channel_id(channel)
        thread_id = self._activity_thread_id(channel)
        stats = await self._activity_store.get_user_channel_stats(
            ctx.guild.id,
            user_id,
            parent_channel_id,
            thread_id,
            thread_id is None,
            end_date,
            days,
        )

        title = (
            f"User channel activity: {self._format_user_reference(ctx.guild, user_id)} - "
            f"{self._format_activity_location(ctx.guild, parent_channel_id, thread_id)}"
        )
        await ctx.send(embed=self._build_user_channel_stats_embed(title, stats, days))

    @nhmisc_usermodstats.command(name="channels")
    async def nhmisc_usermodstats_channels(
        self, ctx: commands.Context, target: str, range_text: str
    ) -> None:
        """Show how a user's activity is distributed across channels."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        user_id = self._parse_user_id(target)
        days = self._parse_range_days(range_text)
        days = await self._cap_detail_days(ctx.guild, days)
        end_date = self._utc_today()
        distribution = await self._activity_store.get_user_channel_distribution(
            ctx.guild.id, user_id, end_date, days
        )

        title = f"User channel distribution: {self._format_user_reference(ctx.guild, user_id)}"
        await ctx.send(
            embed=self._build_user_channel_distribution_embed(ctx.guild, title, distribution, days)
        )

    @nhmisc.command(name="chatchart")
    async def nhmisc_chatchart(
        self,
        ctx: commands.Context,
        target_or_days: str,
        days_or_amount: int | None = None,
        amount: int | None = None,
    ) -> None:
        """Render a chart of user activity in the selected or current channel."""
        await self._require_activity_staff(ctx)
        target, days, amount = self._resolve_chatchart_request(
            ctx,
            target_or_days,
            days_or_amount,
            amount,
        )
        if target is not ctx.channel and not target.permissions_for(ctx.author).view_channel:
            raise commands.UserFeedbackCheckFailure(
                "You cannot view that channel or thread."
            )
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Days must be at least 1.")
        if not 1 <= amount <= MAX_CHATCHART_USER_COUNT:
            raise commands.UserFeedbackCheckFailure(
                f"Amount must be between 1 and {MAX_CHATCHART_USER_COUNT}."
            )

        days = await self._cap_detail_days(ctx.guild, days)
        channel_id = self._activity_parent_channel_id(target)
        counts = await self._activity_store.get_channel_user_counts(
            ctx.guild.id,
            channel_id,
            self._activity_thread_id(target),
            self._utc_today(),
            days,
        )
        if not counts:
            await ctx.send(f"No retained activity data for this channel in the last {days} days.")
            return

        file = self._build_chatchart_file(
            ctx.guild,
            counts,
            days,
            self._chatchart_location_label(target),
            amount,
        )
        content = "One is a bit low, no? 🤨" if amount == 1 else None
        await ctx.send(
            content,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmisc.command(name="topyapper")
    async def nhmisc_topyapper(
        self, ctx: commands.Context, days: int, amount: int
    ) -> None:
        """Show the most active users across this server."""
        await self._send_yapper_ranking(ctx, days, amount)

    @commands.command(name="selfchart")
    @commands.guild_only()
    async def selfchart(self, ctx: commands.Context) -> None:
        """Show your own simplified activity stats for the last 7 retained days."""
        days = await self._cap_detail_days(ctx.guild, 7)
        stats = await self._activity_store.get_user_stats(
            ctx.guild.id, ctx.author.id, self._utc_today(), days
        )
        embed = self._build_selfchart_embed(ctx.author, stats, days)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        """Pin the starter message for a new post in a configured forum."""
        await self._forum_autopin.handle_thread_create(thread)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Drop autopin configuration for a deleted forum."""
        await self._forum_autopin.handle_channel_delete(channel)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Snapshot configured sticky roles when a member leaves."""
        configured_roles = await self._sticky_roles.get_sticky_roles(member.guild.id)
        if not configured_roles:
            await self._send_sticky_debug_log(
                member.guild,
                (
                    "Sticky role snapshot write skipped\n"
                    f"User: {member.mention} (`{member.id}`)\n"
                    "Reason: no sticky roles are configured on this server."
                ),
            )
            return

        current_role_ids = {role.id for role in member.roles}
        saved_role_ids = configured_roles & current_role_ids
        await self._sticky_roles.replace_member_roles(
            member.guild.id,
            member.id,
            saved_role_ids,
        )
        await self._send_sticky_debug_log(
            member.guild,
            (
                "Sticky role snapshot written\n"
                f"User: {member.mention} (`{member.id}`)\n"
                f"Saved roles: {self._format_role_id_set(member.guild, saved_role_ids)}"
            ),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Restore saved sticky roles when a member rejoins."""
        saved_role_ids = await self._sticky_roles.get_member_roles(member.guild.id, member.id)
        if not saved_role_ids:
            await self._send_sticky_debug_log(
                member.guild,
                (
                    "Sticky role snapshot read\n"
                    f"User: {member.mention} (`{member.id}`)\n"
                    "Saved roles: none\n"
                    "Result: nothing to restore."
                ),
            )
            return

        configured_role_ids = await self._sticky_roles.get_sticky_roles(member.guild.id)
        roles: list[discord.Role] = []
        for role_id in sorted(saved_role_ids & configured_role_ids):
            role = member.guild.get_role(role_id)
            if role is not None and self._can_restore_role(member.guild, role):
                roles.append(role)

        restorable_role_ids = {role.id for role in roles}
        skipped_role_ids = saved_role_ids - restorable_role_ids
        if not roles:
            await self._send_sticky_debug_log(
                member.guild,
                (
                    "Sticky role snapshot read\n"
                    f"User: {member.mention} (`{member.id}`)\n"
                    f"Saved roles: {self._format_role_id_set(member.guild, saved_role_ids)}\n"
                    "Restorable roles: none\n"
                    f"Skipped roles: {self._format_role_id_set(member.guild, skipped_role_ids)}\n"
                    "Result: nothing restorable."
                ),
            )
            return

        result = "restored"
        try:
            await member.add_roles(*roles, reason="Restoring sticky roles")
        except discord.Forbidden:
            result = "failed: missing permissions"
            log.warning(
                "Missing permissions to restore sticky roles for member %s in guild %s",
                member.id,
                member.guild.id,
            )
        except discord.HTTPException:
            result = "failed: Discord API error"
            log.exception(
                "Failed to restore sticky roles for member %s in guild %s",
                member.id,
                member.guild.id,
            )
        await self._send_sticky_debug_log(
            member.guild,
            (
                "Sticky role snapshot read\n"
                f"User: {member.mention} (`{member.id}`)\n"
                f"Saved roles: {self._format_role_id_set(member.guild, saved_role_ids)}\n"
                f"Restorable roles: {self._format_role_id_set(member.guild, restorable_role_ids)}\n"
                f"Skipped roles: {self._format_role_id_set(member.guild, skipped_role_ids)}\n"
                f"Result: {result}."
            ),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """Ask how to handle sticky role DB rows when a Discord role is deleted."""
        config_exists, saved_rows = await self._sticky_roles.get_role_state(
            role.guild.id, role.id
        )
        if not config_exists and saved_rows == 0:
            return

        config = await self.config.guild(role.guild).all()
        channel = self._get_log_channel(role.guild, config["sticky_debug_logging_channel"])
        if channel is None:
            log.warning(
                "Sticky role %s was deleted in guild %s but no sticky debug channel is set",
                role.id,
                role.guild.id,
            )
            return

        await self._prompt_sticky_role_db_action(
            guild=role.guild,
            channel=channel,
            role_id=role.id,
            role_name=role.name,
            config_exists=config_exists,
            saved_rows=saved_rows,
            reason="Discord role deletion event",
            requester=None,
        )

    @commands.Cog.listener("on_member_join")
    async def on_role_analytics_member_join(self, member: discord.Member) -> None:
        await self._role_analytics.member_joined(
            member.guild.id,
            member,
            member.guild.default_role.id,
        )

    @commands.Cog.listener("on_member_update")
    async def on_role_analytics_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        if before_role_ids == after_role_ids:
            return
        await self._role_analytics.member_roles_changed(
            after.guild.id,
            after,
            after.guild.default_role.id,
        )

    @commands.Cog.listener("on_member_remove")
    async def on_role_analytics_member_remove(self, member: discord.Member) -> None:
        await self._role_analytics.member_removed(member.guild.id, member.id)

    @commands.Cog.listener("on_guild_role_delete")
    async def on_role_analytics_role_delete(self, role: discord.Role) -> None:
        await self._role_analytics.role_deleted(role.guild.id, role.id)

    @commands.Cog.listener("on_resumed")
    async def on_role_analytics_resumed(self) -> None:
        await self._role_analytics.schedule_resumed_check(tuple(self.bot.guilds))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Passively collect message activity counters."""
        guild = message.guild
        if guild is None:
            return
        if message.author.bot or message.webhook_id is not None:
            return
        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return

        now = datetime.now(timezone.utc)
        today = now.date()
        await self._close_stale_activity_days_for_guild(guild, send_reports=True)
        await self._activity_store.record_message(
            guild.id,
            today,
            now.hour,
            message.author.id,
            self._activity_parent_channel_id(message.channel),
            self._activity_thread_id(message.channel),
            now,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Log voice channel joins, leaves, moves, and VC jumping."""
        if before.channel == after.channel:
            return

        guild = member.guild
        config = await self.config.guild(guild).all()
        log_channel = self._get_log_channel(guild, config["voice_log_channel"])
        event_timestamp = int(time.time())

        if log_channel is not None:
            if before.channel is None and after.channel is not None:
                await self._send_voice_log(
                    log_channel,
                    (
                        f"{member.mention} ({member.id}) has joined a channel "
                        f"{after.channel.mention} at <t:{event_timestamp}:F>"
                    ),
                )
            elif before.channel is not None and after.channel is None:
                await self._send_voice_log(
                    log_channel,
                    (
                        f"{member.mention} ({member.id}) has left a channel "
                        f"{before.channel.mention} at <t:{event_timestamp}:F>"
                    ),
                )
            elif before.channel is not None and after.channel is not None:
                move_log_content = (
                    f"{member.mention} ({member.id}) has moved from "
                    f"{before.channel.mention} to {after.channel.mention} "
                    f"at <t:{event_timestamp}:F>"
                )
                move_log_message = await self._send_voice_log(
                    log_channel,
                    move_log_content,
                )
                if move_log_message is not None:
                    self._schedule_audit_log_edit(
                        move_log_message,
                        move_log_content,
                        guild,
                        member,
                        after.channel,
                        event_timestamp,
                    )

        if after.channel is None:
            return

        is_vcjumping = self._voice_visits.record_visit(
            (guild.id, member.id),
            after.channel.id,
            timestamp=time.monotonic(),
            visit_count=config["vcjumping_visit_count"],
            window_seconds=config["vcjumping_window_seconds"],
        )
        if is_vcjumping:
            alert_channel = self._get_log_channel(guild, config["alert_channel"])
            if alert_channel is None:
                return

            await self._send_voice_log(
                alert_channel,
                (
                    f"{member.mention} is VC jumping "
                    f"({config['vcjumping_visit_count']} channel entries in "
                    f"{config['vcjumping_window_seconds']} seconds)."
                ),
            )

    def _get_log_channel(
        self, guild: discord.Guild, channel_id: int | None
    ) -> discord.TextChannel | None:
        if channel_id is None:
            return None

        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    def _missing_log_permissions(
        self, guild: discord.Guild, channel: discord.TextChannel
    ) -> str | None:
        me = guild.me
        permissions = channel.permissions_for(me)
        if not permissions.view_channel:
            return f"I need permission to view {channel.mention}."
        if not permissions.send_messages:
            return f"I need permission to send messages in {channel.mention}."
        return None

    async def _send_guild_alert(self, guild: discord.Guild, content: str) -> bool:
        """Send to the configured alert channel. False when there is none."""
        alert_channel = self._get_log_channel(
            guild, await self.config.guild(guild).alert_channel()
        )
        if alert_channel is None:
            return False

        await self._send_voice_log(alert_channel, content)
        return True

    def _schedule_audit_log_edit(
        self,
        message: discord.Message,
        base_content: str,
        guild: discord.Guild,
        member: discord.Member,
        after_channel: discord.VoiceChannel | discord.StageChannel,
        event_timestamp: int,
    ) -> None:
        task = asyncio.create_task(
            self._edit_move_log_with_moderator(
                message,
                base_content,
                guild,
                member,
                after_channel,
                event_timestamp,
            )
        )
        self._audit_log_tasks.add(task)
        task.add_done_callback(self._audit_log_tasks.discard)

    async def _edit_move_log_with_moderator(
        self,
        message: discord.Message,
        base_content: str,
        guild: discord.Guild,
        member: discord.Member,
        after_channel: discord.VoiceChannel | discord.StageChannel,
        event_timestamp: int,
    ) -> None:
        for attempt in range(5):
            if attempt > 0:
                await asyncio.sleep(2)

            moved_by = await self._get_voice_move_moderator(
                guild, member, after_channel, event_timestamp
            )
            if moved_by is None:
                continue

            try:
                timestamp_suffix = f" at <t:{event_timestamp}:F>"
                edited_content = base_content.replace(
                    timestamp_suffix,
                    f" moved by {self._format_user_label(moved_by)}{timestamp_suffix}",
                    1,
                )
                await message.edit(
                    content=edited_content,
                )
            except discord.HTTPException:
                log.exception("Failed to edit voice move log message %s", message.id)
            return

    async def _get_voice_move_moderator(
        self,
        guild: discord.Guild,
        member: discord.Member,
        after_channel: discord.VoiceChannel | discord.StageChannel,
        event_timestamp: int,
    ) -> discord.User | discord.Member | None:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None

        event_time = datetime.fromtimestamp(event_timestamp, timezone.utc)
        try:
            async for entry in guild.audit_logs(
                limit=5,
                action=discord.AuditLogAction.member_move,
            ):
                created_at = entry.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)

                if abs((created_at - event_time).total_seconds()) > 15:
                    continue

                target_id = getattr(entry.target, "id", None)
                if target_id == member.id:
                    return entry.user

                extra = getattr(entry, "extra", None)
                extra_channel = getattr(extra, "channel", None)
                extra_count = getattr(extra, "count", None)
                if (
                    target_id is None
                    and getattr(extra_channel, "id", None) == after_channel.id
                    and str(extra_count) == "1"
                ):
                    return entry.user
        except discord.Forbidden:
            return None
        except discord.HTTPException:
            log.exception("Failed to read audit log for voice move in guild %s", guild.id)
        return None

    def _format_user_label(self, user: discord.User | discord.Member) -> str:
        name = getattr(user, "display_name", None) or str(user)
        return f"{name} ({user.id})"

    async def _send_voice_log(
        self, channel: discord.TextChannel, content: str
    ) -> discord.Message | None:
        try:
            return await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Failed to send voice log message to channel %s", channel.id)
        return None

    async def _activity_midnight_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._close_stale_activity_days_for_all_guilds(send_reports=True)
            except Exception:
                log.exception("Failed to close stale activity days")
            try:
                now = datetime.now(timezone.utc)
                next_midnight = datetime.combine(
                    now.date() + timedelta(days=1),
                    datetime_time.min,
                    tzinfo=timezone.utc,
                ) + timedelta(seconds=5)
                await asyncio.sleep(max(1.0, (next_midnight - now).total_seconds()))
            except asyncio.CancelledError:
                raise

    async def _close_stale_activity_days_for_all_guilds(self, send_reports: bool) -> None:
        for guild in list(self.bot.guilds):
            await self._close_stale_activity_days_for_guild(guild, send_reports=send_reports)

    async def _close_stale_activity_days_for_guild(
        self, guild: discord.Guild, send_reports: bool
    ) -> None:
        today = self._utc_today()
        summaries = await self._activity_store.close_stale_days(
            guild.id, today, guild.member_count or 0
        )
        if not summaries:
            return

        config = await self.config.guild(guild).all()
        channel = self._get_log_channel(guild, config["activity_channel"])
        for summary in summaries:
            if send_reports and channel is not None:
                await self._send_activity_summary(channel, summary)
            await self._apply_activity_history_retention(
                guild.id, int(config["activity_history_retention_days"]), summary.date_utc
            )

        await self._apply_activity_detail_retention(
            guild.id, int(config["activity_detail_retention_days"])
        )

    async def _send_activity_summary(
        self, channel: discord.TextChannel, summary: DailySummary
    ) -> None:
        try:
            await channel.send(
                embed=self._build_daily_summary_embed(summary, title_prefix="Daily"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Failed to send activity summary to channel %s", channel.id)

    async def _apply_activity_detail_retention(self, guild_id: int, days: int) -> None:
        if days < 1:
            return
        cutoff = self._utc_today() - timedelta(days=days - 1)
        await self._activity_store.prune_detail_rows_older_than(guild_id, cutoff)

    async def _apply_activity_history_retention(
        self, guild_id: int, days: int, closed_date: date
    ) -> None:
        if days == -1:
            return
        if days == 0:
            await self._activity_store.delete_history_for_date(guild_id, closed_date)
            return
        cutoff = self._utc_today() - timedelta(days=days)
        await self._activity_store.prune_history_rows_older_than(guild_id, cutoff)

    async def _require_manage_guild(self, ctx: commands.Context) -> None:
        permissions = getattr(ctx.author, "guild_permissions", None)
        has_permission = bool(permissions and permissions.manage_guild)
        if has_permission or await self.bot.is_admin(ctx.author):
            return
        raise commands.UserFeedbackCheckFailure("You need Manage Server permission.")

    async def _require_activity_staff(self, ctx: commands.Context) -> None:
        permissions = getattr(ctx.author, "guild_permissions", None)
        has_permission = bool(
            permissions and (permissions.manage_messages or permissions.manage_guild)
        )
        if has_permission or await self.bot.is_admin(ctx.author):
            return
        raise commands.UserFeedbackCheckFailure(
            "You need Manage Messages or Manage Server permission."
        )

    def _prepare_role_expression(
        self, guild: discord.Guild, expression: str
    ) -> tuple[object, str, tuple[int, ...]]:
        try:
            parsed = parse_role_expression(expression)
        except RoleExpressionSyntaxError as error:
            raise commands.UserFeedbackCheckFailure(
                "Invalid role expression"
            ) from error

        for role_id in role_ids(parsed):
            role = guild.get_role(role_id)
            if role is None:
                raise commands.UserFeedbackCheckFailure(
                    "Role expression contains an unknown role"
                )
            if role_id == guild.default_role.id or role.is_default():
                raise commands.UserFeedbackCheckFailure(
                    "The @everyone role cannot be used in role expressions"
                )

        predicate_sql, parameters = compile_role_expression(parsed)
        return parsed, predicate_sql, parameters

    async def _count_role_expression(
        self, guild: discord.Guild, expression: str
    ) -> int:
        _, predicate_sql, parameters = self._prepare_role_expression(
            guild, expression
        )
        try:
            return await self._role_analytics_store.count_matching(
                guild.id, predicate_sql, parameters
            )
        except AnalyticsUnavailableError as error:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            ) from error

    async def _count_highest_role_buckets(
        self, guild: discord.Guild, ordered_role_ids: tuple[int, ...]
    ) -> list[int]:
        counts = []
        for index, role_id in enumerate(ordered_role_ids):
            higher_role_ids = ordered_role_ids[index + 1 :]
            expression = str(role_id)
            if higher_role_ids:
                higher_roles = " OR ".join(
                    str(higher_role_id) for higher_role_id in higher_role_ids
                )
                expression = f"{expression} AND NOT ({higher_roles})"
            counts.append(await self._count_role_expression(guild, expression))
        return counts

    def _require_private_role_export_channel(self, ctx: commands.Context) -> None:
        if self._channel_is_public(ctx):
            log.info(
                "Role export refused in public channel %s for guild %s",
                getattr(ctx.channel, "id", "unknown"),
                ctx.guild.id,
            )
            raise commands.UserFeedbackCheckFailure(
                "Role export is unavailable in this channel"
            )

        if ctx.guild.me is None:
            missing_permissions = ("bot_member",)
        else:
            bot_permissions = ctx.channel.permissions_for(ctx.guild.me)
            missing_permissions = tuple(
                name
                for name in ("view_channel", "send_messages", "attach_files")
                if not bool(getattr(bot_permissions, name, False))
            )
        if missing_permissions:
            log.warning(
                "Role export refused in channel %s for guild %s; missing bot permissions: %s",
                getattr(ctx.channel, "id", "unknown"),
                ctx.guild.id,
                ", ".join(missing_permissions),
            )
            raise commands.UserFeedbackCheckFailure(
                "Role export is unavailable in this channel"
            )

    async def _repair_role_analytics_cache(self, guild: discord.Guild) -> None:
        await self._role_analytics_store.set_status(
            guild.id,
            SyncStatus.NEEDS_RECONCILIATION,
            "member_cache_mismatch",
        )
        self._role_analytics.schedule_guild_retry(guild, 0)

    def _parse_role_id(self, value: str) -> int:
        stripped = value.strip()
        if stripped.startswith("<@&") and stripped.endswith(">"):
            stripped = stripped[3:-1]
        if not stripped.isdigit():
            raise commands.UserFeedbackCheckFailure("Pass a role mention or raw Discord role ID.")
        return int(stripped)

    def _can_restore_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        me = guild.me
        if me is None:
            return False
        if role.is_default() or role.managed:
            return False
        if not me.guild_permissions.manage_roles:
            return False
        return role < me.top_role

    def _format_role_reference(self, guild: discord.Guild, role_id: int) -> str:
        role = guild.get_role(role_id)
        if role is None:
            return f"`{role_id}` (missing)"
        return f"{role.mention} (`{role_id}`)"

    def _format_role_id_set(self, guild: discord.Guild, role_ids: set[int]) -> str:
        if not role_ids:
            return "none"
        return ", ".join(
            self._format_role_reference(guild, role_id) for role_id in sorted(role_ids)
        )

    def _role_name_for_prompt(self, guild: discord.Guild, role_id: int) -> str | None:
        role = guild.get_role(role_id)
        if role is None:
            return None
        return role.name

    async def _prompt_sticky_role_db_action(
        self,
        *,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        role_id: int,
        role_name: str | None,
        config_exists: bool,
        saved_rows: int,
        reason: str,
        requester: discord.Member | discord.User | None,
    ) -> None:
        role_label = f"{role_name} (`{role_id}`)" if role_name else f"`{role_id}`"
        await channel.send(
            "Sticky role DB entry needs a decision.\n"
            f"Role: {role_label}\n"
            f"Trigger: {reason}\n"
            f"Configured as sticky: {'yes' if config_exists else 'no'}\n"
            f"Saved user-role rows: {saved_rows}\n"
            "Reply with one of:\n"
            "`remove` - delete this role from sticky DB and saved users\n"
            "`keep` - stop configuring this role as sticky, but keep saved user rows\n"
            "`change <role mention or ID>` - move config and saved users to another role",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        deadline = time.monotonic() + 300
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await channel.send("Sticky role DB decision timed out. No changes were made.")
                return

            def check(message: discord.Message) -> bool:
                return message.channel.id == channel.id and not message.author.bot

            try:
                message = await self.bot.wait_for("message", check=check, timeout=remaining)
            except asyncio.TimeoutError:
                await channel.send("Sticky role DB decision timed out. No changes were made.")
                return

            if not await self._can_answer_sticky_db_prompt(message, guild, requester):
                continue

            content = message.content.strip()
            command, _, argument = content.partition(" ")
            command = command.lower()
            if command == "remove" and not argument:
                config_removed, rows_removed = await self._sticky_roles.remove_sticky_role(
                    guild.id, role_id
                )
                await channel.send(
                    "Sticky role DB entry removed.\n"
                    f"Config row removed: {'yes' if config_removed else 'no'}\n"
                    f"Saved user-role rows removed: {rows_removed}"
                )
                return
            if command == "keep" and not argument:
                config_removed = await self._sticky_roles.unconfigure_sticky_role(
                    guild.id, role_id
                )
                await channel.send(
                    "Sticky role config removed, saved user-role rows kept.\n"
                    f"Config row removed: {'yes' if config_removed else 'no'}\n"
                    f"Saved user-role rows kept: {saved_rows}"
                )
                return
            if command == "change":
                await self._handle_sticky_role_db_change(
                    channel, guild, role_id, argument.strip()
                )
                return

            await channel.send(
                "Invalid response. Use `remove`, `keep`, or `change <role mention or ID>`."
            )

    async def _can_answer_sticky_db_prompt(
        self,
        message: discord.Message,
        guild: discord.Guild,
        requester: discord.Member | discord.User | None,
    ) -> bool:
        if requester is not None:
            return message.author.id == requester.id

        member = message.author
        if not isinstance(member, discord.Member):
            member = guild.get_member(message.author.id)
        permissions = getattr(member, "guild_permissions", None)
        if permissions and permissions.manage_guild:
            return True
        return await self.bot.is_admin(message.author)

    async def _handle_sticky_role_db_change(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        old_role_id: int,
        role_argument: str,
    ) -> None:
        if not role_argument:
            await channel.send("Missing replacement role. No changes were made.")
            return

        try:
            new_role_id = self._parse_role_id(role_argument)
        except commands.UserFeedbackCheckFailure as exc:
            await channel.send(f"{exc} No changes were made.")
            return

        if new_role_id == old_role_id:
            await channel.send("Replacement role is the same role ID. No changes were made.")
            return

        new_role = guild.get_role(new_role_id)
        if new_role is None:
            await channel.send("Replacement role does not exist on this server. No changes were made.")
            return
        if not self._can_restore_role(guild, new_role):
            await channel.send(
                "I cannot restore the replacement role. Check Manage Roles and role hierarchy. "
                "No changes were made."
            )
            return

        config_moved, old_rows_removed, new_rows_inserted = await self._sticky_roles.replace_sticky_role(
            guild.id, old_role_id, new_role_id
        )
        await channel.send(
            "Sticky role DB entry changed.\n"
            f"Replacement role: {new_role.mention} (`{new_role.id}`)\n"
            f"Config moved: {'yes' if config_moved else 'no'}\n"
            f"Old saved user-role rows removed: {old_rows_removed}\n"
            f"New saved user-role rows inserted: {new_rows_inserted}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_sticky_debug_log(self, guild: discord.Guild, content: str) -> None:
        config = await self.config.guild(guild).all()
        if not config["sticky_debug_logging_enabled"]:
            return

        channel = self._get_log_channel(guild, config["sticky_debug_logging_channel"])
        if channel is None:
            return

        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Failed to send sticky role debug log to guild %s", guild.id)

    async def _send_paginated_text(self, ctx: commands.Context, content: str) -> None:
        page = ""
        for line in content.splitlines():
            candidate = f"{page}\n{line}" if page else line
            if len(candidate) > 1900:
                await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())
                page = line
            else:
                page = candidate
        if page:
            await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())

    async def _send_yapper_ranking(
        self,
        ctx: commands.Context,
        days: int,
        amount: int,
    ) -> None:
        await self._require_activity_staff(ctx)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Days must be at least 1.")
        if not 1 <= amount <= 20:
            raise commands.UserFeedbackCheckFailure(
                "Amount must be between 1 and 20."
            )

        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        days = await self._cap_detail_days(ctx.guild, days)
        end_date_utc = self._utc_today()
        counts = await self._activity_store.get_guild_user_counts(
            ctx.guild.id,
            end_date_utc,
            days,
            amount,
        )
        scope = "server"

        if not counts:
            await ctx.send(
                f"No retained activity data for this {scope} in the last {days} days.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        lines = [f"Top {len(counts)} yappers in this {scope} - last {days} days:"]
        for rank, count in enumerate(counts, start=1):
            member = ctx.guild.get_member(count.user_id)
            user = (
                f"{member.display_name} ({count.user_id})"
                if member is not None
                else str(count.user_id)
            )
            lines.append(f"{rank}. {user} — {count.message_count:,} messages")
        await self._send_paginated_text(ctx, "\n".join(lines))

    async def _confirm_retention_delete(self, ctx: commands.Context, warning: str) -> bool:
        await ctx.send(warning)

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
                and message.content == RETENTION_CONFIRMATION
            )

        try:
            await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await ctx.send("Retention change cancelled.")
            return False
        return True

    def _utc_today(self) -> date:
        return datetime.now(timezone.utc).date()

    def _history_retention_cutoff(self, days: int) -> date | None:
        if days == -1:
            return None
        if days == 0:
            return self._utc_today() + timedelta(days=1)
        return self._utc_today() - timedelta(days=days)

    async def _cap_detail_days(self, guild: discord.Guild, days: int) -> int:
        config = await self.config.guild(guild).all()
        retention = max(1, int(config["activity_detail_retention_days"]))
        return min(days, retention)

    def _parse_range_days(self, value: str) -> int:
        normalized = value.strip().lower()
        if not normalized.isdigit():
            raise commands.UserFeedbackCheckFailure("Range must be a positive number of days.")
        days = int(normalized)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Range must be at least 1 day.")
        return days

    def _parse_user_id(self, value: str) -> int:
        stripped = value.strip()
        if stripped.startswith("<@") and stripped.endswith(">"):
            stripped = stripped[2:-1]
            if stripped.startswith("!"):
                stripped = stripped[1:]
        if not stripped.isdigit():
            raise commands.UserFeedbackCheckFailure("Pass a user mention or raw Discord user ID.")
        return int(stripped)

    def _resolve_text_channel_or_thread(
        self, guild: discord.Guild, value: str
    ) -> discord.TextChannel | discord.Thread:
        stripped = value.strip()
        if stripped.startswith("<#") and stripped.endswith(">"):
            stripped = stripped[2:-1]
        if not stripped.isdigit():
            raise commands.UserFeedbackCheckFailure("Pass a channel/thread mention or raw channel ID.")

        channel_id = int(stripped)
        channel = guild.get_channel_or_thread(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        raise commands.UserFeedbackCheckFailure("Channel or thread was not found in this server.")

    def _resolve_chatchart_request(
        self,
        ctx: commands.Context,
        target_or_days: str,
        days_or_amount: int | None,
        amount: int | None,
    ) -> tuple[discord.TextChannel | discord.Thread, int, int]:
        token = str(target_or_days).strip()
        resolved_channel = None
        if token.isdigit() and len(token) >= DISCORD_SNOWFLAKE_MIN_DIGITS:
            resolved_channel = ctx.guild.get_channel_or_thread(int(token))

        is_channel_reference = (
            token.startswith("<#")
            or (token.isdigit() and len(token) >= DISCORD_SNOWFLAKE_MIN_DIGITS)
            or isinstance(resolved_channel, (discord.TextChannel, discord.Thread))
        )
        if is_channel_reference:
            target = self._resolve_text_channel_or_thread(ctx.guild, token)
            if days_or_amount is None:
                raise commands.UserFeedbackCheckFailure(
                    "Days must follow the channel or thread."
                )
            return (
                target,
                days_or_amount,
                amount if amount is not None else DEFAULT_CHATCHART_USER_COUNT,
            )

        if not token.isdigit():
            raise commands.UserFeedbackCheckFailure(
                "Pass a channel/thread mention, raw channel ID, or number of days."
            )
        if amount is not None:
            raise commands.UserFeedbackCheckFailure(
                "Too many arguments for current-channel chatchart."
            )
        return (
            ctx.channel,
            int(token),
            days_or_amount if days_or_amount is not None else DEFAULT_CHATCHART_USER_COUNT,
        )

    def _activity_parent_channel_id(self, channel: object) -> int:
        parent = getattr(channel, "parent", None)
        if isinstance(channel, discord.Thread) and parent is not None:
            return parent.id
        return channel.id

    def _activity_thread_id(self, channel: object) -> int | None:
        if isinstance(channel, discord.Thread):
            return channel.id
        return None

    def _format_channel(self, channel_id: int) -> str:
        return f"<#{channel_id}>"

    def _format_activity_location(
        self, guild: discord.Guild, channel_id: int, thread_id: int | None
    ) -> str:
        if thread_id is None:
            return self._format_channel(channel_id)
        return f"{self._format_channel(channel_id)} / {self._format_channel(thread_id)}"

    def _format_user_reference(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member is not None:
            return f"{member.display_name} ({user_id})"
        return f"<@{user_id}> ({user_id})"

    def _format_int(self, value: int) -> str:
        return f"{value:,}"

    def _format_percent_of_server(self, active_users: int, member_count: int) -> str:
        if member_count <= 0:
            return "n/d"
        return f"{(active_users / member_count) * 100:.1f}%"

    def _format_top_channels(self, top_channels: list[TopChannel]) -> str:
        if not top_channels:
            return "n/d"
        return "\n".join(
            (
                f"{top.rank}. {self._format_channel(top.channel_id)} - "
                f"{self._format_int(top.message_count)} messages"
            )
            for top in top_channels
        )

    def _format_activity_locations(
        self, guild: discord.Guild, locations: list[ActivityLocation], total_messages: int
    ) -> str:
        if not locations:
            return "n/d"
        lines: list[str] = []
        listed_total = 0
        for location in locations:
            listed_total += location.message_count
            percent = (
                (location.message_count / total_messages) * 100.0
                if total_messages
                else 0.0
            )
            lines.append(
                f"{location.rank}. "
                f"{self._format_activity_location(guild, location.channel_id, location.thread_id)} - "
                f"{self._format_int(location.message_count)} ({percent:.1f}%)"
            )
        other_count = total_messages - listed_total
        if other_count > 0:
            percent = (other_count / total_messages) * 100.0 if total_messages else 0.0
            lines.append(f"Other - {self._format_int(other_count)} ({percent:.1f}%)")
        return "\n".join(lines)

    def _build_daily_summary_embed(
        self, summary: DailySummary, title_prefix: str
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{title_prefix} activity summary - {summary.date_utc.isoformat()} UTC",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Messages",
            value=self._format_int(summary.total_messages),
            inline=True,
        )
        embed.add_field(
            name="Active users",
            value=(
                f"{self._format_int(summary.active_users)} "
                f"({self._format_percent_of_server(summary.active_users, summary.member_count_at_close)})"
            ),
            inline=True,
        )
        embed.add_field(
            name="Thresholds",
            value=(
                f"10+: {self._format_int(summary.users_10_plus)}\n"
                f"50+: {self._format_int(summary.users_50_plus)}\n"
                f"100+: {self._format_int(summary.users_100_plus)}"
            ),
            inline=True,
        )
        peak_hour = self._format_peak_hour(summary)
        embed.add_field(
            name="Channels",
            value=(
                f"Active: {self._format_int(summary.channels_with_activity)}\n"
                f"Peak hour: {peak_hour}\n"
                f"Avg/user: {summary.messages_per_active_user:.1f}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Top channels",
            value=self._format_top_channels(summary.top_channels),
            inline=False,
        )
        return embed

    def _format_peak_hour(self, summary: DailySummary) -> str:
        if summary.peak_hour_utc is None:
            return "n/d"
        peak_time = datetime(
            summary.date_utc.year,
            summary.date_utc.month,
            summary.date_utc.day,
            summary.peak_hour_utc,
            tzinfo=timezone.utc,
        )
        return f"<t:{int(peak_time.timestamp())}:t>"

    def _build_timeline_embed(
        self, timeline: list[TimelineDay], top_channels: list[TopChannel], days: int
    ) -> discord.Embed:
        include_percent = days <= 7
        header = "Date       Msgs  Users  %Srv  10+ 50+ 100+" if include_percent else "Date       Msgs  Users  10+ 50+ 100+"
        lines = [header]
        summaries: list[DailySummary] = []
        for day in timeline:
            summary = day.summary
            if summary is None:
                if include_percent:
                    lines.append(f"{day.date_utc.isoformat()} n/d   n/d    n/d   n/d n/d n/d")
                else:
                    lines.append(f"{day.date_utc.isoformat()} n/d   n/d    n/d n/d n/d")
                continue
            summaries.append(summary)
            if include_percent:
                lines.append(
                    f"{day.date_utc.isoformat()} "
                    f"{summary.total_messages:<5} {summary.active_users:<6} "
                    f"{self._format_percent_of_server(summary.active_users, summary.member_count_at_close):<5} "
                    f"{summary.users_10_plus:<3} {summary.users_50_plus:<3} {summary.users_100_plus:<4}"
                )
            else:
                lines.append(
                    f"{day.date_utc.isoformat()} "
                    f"{summary.total_messages:<5} {summary.active_users:<6} "
                    f"{summary.users_10_plus:<3} {summary.users_50_plus:<3} {summary.users_100_plus:<4}"
                )

        table = "\n".join(lines)
        if len(table) > 3900:
            visible_lines = lines[:120]
            visible_lines.append("...")
            table = "\n".join(visible_lines)

        embed = discord.Embed(
            title=f"Activity timeline - last {days} closed days",
            color=discord.Color.blue(),
            description=f"```text\n{table}\n```",
        )
        if summaries:
            avg_messages = sum(summary.total_messages for summary in summaries) / len(summaries)
            avg_users = sum(summary.active_users for summary in summaries) / len(summaries)
            best = max(summaries, key=lambda summary: summary.total_messages)
            embed.add_field(
                name="Range",
                value=(
                    f"Avg/day: {avg_messages:.0f} msgs\n"
                    f"Avg active users: {avg_users:.0f}\n"
                    f"Best day: {best.date_utc.isoformat()} "
                    f"({self._format_int(best.total_messages)} msgs)"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Range", value="n/d", inline=False)
        embed.add_field(
            name="Top channels in range",
            value=self._format_top_channels(top_channels),
            inline=False,
        )
        return embed

    def _build_channel_timeline_embed(
        self, channel: discord.TextChannel, timeline: list[ChannelTimelineDay], days: int
    ) -> discord.Embed:
        lines = ["Date       Msgs"]
        numeric_counts: list[int] = []
        for day in timeline:
            if day.message_count is None:
                value = "n/d"
            else:
                numeric_counts.append(day.message_count)
                value = str(day.message_count)
            lines.append(f"{day.date_utc.isoformat()} {value}")

        table = "\n".join(lines)
        if len(table) > 3900:
            visible_lines = lines[:120]
            visible_lines.append("...")
            table = "\n".join(visible_lines)

        embed = discord.Embed(
            title=f"Channel activity - {channel.name} - last {days} days",
            color=discord.Color.blue(),
            description=f"```text\n{table}\n```",
        )
        if numeric_counts:
            total = sum(numeric_counts)
            active_days = sum(1 for value in numeric_counts if value > 0)
            embed.add_field(name="Total messages", value=self._format_int(total), inline=True)
            embed.add_field(name="Active days", value=self._format_int(active_days), inline=True)
            embed.add_field(
                name="Average per active day",
                value=f"{(total / active_days) if active_days else 0.0:.1f}",
                inline=True,
            )
        else:
            embed.add_field(name="Total messages", value="n/d", inline=True)
        return embed

    def _build_activity_consistency_embed(
        self, report: ActivityConsistencyReport, day: date
    ) -> discord.Embed:
        ok = report.user_day_mismatches == 0 and report.channel_day_mismatches == 0
        embed = discord.Embed(
            title=f"Activity consistency - {day.isoformat()} UTC",
            color=discord.Color.green() if ok else discord.Color.red(),
        )
        embed.add_field(
            name="Canonical rows",
            value=self._format_int(report.canonical_rows),
            inline=True,
        )
        embed.add_field(
            name="Canonical messages",
            value=self._format_int(report.canonical_messages),
            inline=True,
        )
        embed.add_field(
            name="User cache mismatches",
            value=self._format_int(report.user_day_mismatches),
            inline=True,
        )
        embed.add_field(
            name="Channel cache mismatches",
            value=self._format_int(report.channel_day_mismatches),
            inline=True,
        )
        embed.add_field(name="Status", value="OK" if ok else "Mismatch detected", inline=False)
        return embed

    def _build_activity_database_stats_embed(
        self, stats: ActivityDatabaseStats
    ) -> discord.Embed:
        file_mib = stats.file_size_bytes / 1024 / 1024
        sqlite_mib = stats.sqlite_size_bytes / 1024 / 1024
        lines = [f"{name} {self._format_int(count)}" for name, count in stats.table_rows]
        table = "\n".join(lines)
        if len(table) > 1000:
            table = "\n".join(lines[:18] + ["..."])

        embed = discord.Embed(title="Activity database size", color=discord.Color.blue())
        embed.add_field(
            name="File size",
            value=f"{self._format_int(stats.file_size_bytes)} bytes ({file_mib:.2f} MiB)",
            inline=False,
        )
        embed.add_field(
            name="SQLite pages",
            value=(
                f"{self._format_int(stats.page_count)} pages x "
                f"{self._format_int(stats.page_size)} bytes = {sqlite_mib:.2f} MiB"
            ),
            inline=False,
        )
        embed.add_field(name="Rows", value=f"```text\n{table}\n```", inline=False)
        embed.set_footer(text=stats.path)
        return embed

    def _build_user_stats_embed(self, title: str, stats: UserStats, days: int) -> discord.Embed:
        embed = discord.Embed(title=title, color=discord.Color.blue())
        embed.add_field(name="Range", value=f"last {days} days", inline=True)
        embed.add_field(name="Total messages", value=self._format_int(stats.total_messages), inline=True)
        embed.add_field(name="Active days", value=self._format_int(stats.active_days), inline=True)
        embed.add_field(
            name="Average per active day",
            value=f"{stats.average_per_active_day:.1f}",
            inline=True,
        )
        embed.add_field(
            name="Top channels",
            value=self._format_top_channels(stats.top_channels),
            inline=False,
        )
        embed.add_field(
            name="Daily breakdown",
            value=f"```text\n{self._format_daily_rows(stats.date_rows)}\n```",
            inline=False,
        )
        return embed

    def _build_user_channel_stats_embed(
        self, title: str, stats: UserStats, days: int
    ) -> discord.Embed:
        embed = discord.Embed(title=title, color=discord.Color.blue())
        embed.add_field(name="Range", value=f"last {days} days", inline=True)
        embed.add_field(name="Total messages", value=self._format_int(stats.total_messages), inline=True)
        embed.add_field(name="Active days", value=self._format_int(stats.active_days), inline=True)
        embed.add_field(
            name="Average per active day",
            value=f"{stats.average_per_active_day:.1f}",
            inline=True,
        )
        embed.add_field(
            name="Daily breakdown",
            value=f"```text\n{self._format_daily_rows(stats.date_rows)}\n```",
            inline=False,
        )
        return embed

    def _build_user_channel_distribution_embed(
        self,
        guild: discord.Guild,
        title: str,
        distribution: UserChannelDistribution,
        days: int,
    ) -> discord.Embed:
        embed = discord.Embed(title=title, color=discord.Color.blue())
        embed.add_field(name="Range", value=f"last {days} days", inline=True)
        embed.add_field(
            name="Total messages",
            value=self._format_int(distribution.total_messages),
            inline=True,
        )
        embed.add_field(
            name="Active days",
            value=self._format_int(distribution.active_days),
            inline=True,
        )
        embed.add_field(
            name="Locations used",
            value=self._format_int(distribution.locations_used),
            inline=True,
        )
        top_location = distribution.top_locations[0] if distribution.top_locations else None
        embed.add_field(
            name="Top location",
            value=(
                self._format_activity_location(guild, top_location.channel_id, top_location.thread_id)
                if top_location
                else "n/d"
            ),
            inline=True,
        )
        embed.add_field(
            name="Top locations in range",
            value=self._format_activity_locations(
                guild, distribution.top_locations, distribution.total_messages
            ),
            inline=False,
        )
        embed.add_field(
            name="Daily dominant location",
            value=self._format_daily_dominant_location_rows(guild, distribution.date_rows),
            inline=False,
        )
        return embed

    def _build_selfchart_embed(
        self, member: discord.Member | discord.User, stats: UserStats, days: int
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Your activity - last {days} days",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Total messages", value=self._format_int(stats.total_messages), inline=True)
        top_channel = stats.top_channels[0] if stats.top_channels else None
        embed.add_field(
            name="Top channel",
            value=(
                f"{self._format_channel(top_channel.channel_id)} - "
                f"{self._format_int(top_channel.message_count)} messages"
                if top_channel
                else "n/d"
            ),
            inline=True,
        )
        embed.add_field(
            name="Daily messages",
            value=f"```text\n{self._format_daily_rows(stats.date_rows)}\n```",
            inline=False,
        )
        return embed

    def _format_daily_rows(self, rows: list[tuple[date, int | None]]) -> str:
        lines = ["Date       Msgs"]
        for day, count in rows:
            value = "n/d" if count is None else str(count)
            lines.append(f"{day.isoformat()} {value}")
        return "\n".join(lines)

    def _format_daily_dominant_location_rows(
        self, guild: discord.Guild, rows: list[DailyDominantLocation]
    ) -> str:
        lines: list[str] = []
        for row in rows:
            if row.total_messages is None:
                lines.append(f"{row.date_utc.isoformat()}: n/d")
            elif row.total_messages == 0:
                lines.append(f"{row.date_utc.isoformat()}: 0")
            elif row.channel_id is not None and row.location_messages is not None:
                percent = (row.location_messages / row.total_messages) * 100.0
                lines.append(
                    f"{row.date_utc.isoformat()}: "
                    f"{self._format_int(row.total_messages)} msgs, "
                    f"{self._format_activity_location(guild, row.channel_id, row.thread_id)} "
                    f"{self._format_int(row.location_messages)} ({percent:.1f}%)"
                )
            else:
                lines.append(f"{row.date_utc.isoformat()}: {self._format_int(row.total_messages)} msgs")
        return self._join_limited_lines(lines)

    def _join_limited_lines(self, lines: list[str], limit: int = 1000) -> str:
        output: list[str] = []
        current_length = 0
        for line in lines:
            extra_length = len(line) + (1 if output else 0)
            if current_length + extra_length > limit:
                output.append("...")
                break
            output.append(line)
            current_length += extra_length
        return "\n".join(output) if output else "n/d"

    def _chatchart_location_label(self, channel: object) -> str:
        """Name the charted channel or thread for display inside the image."""
        name = getattr(channel, "name", None) or "unknown-channel"
        if self._activity_thread_id(channel) is None:
            return f"#{name}"
        parent_name = getattr(getattr(channel, "parent", None), "name", None)
        return f"#{parent_name} / {name}" if parent_name else name

    def _draw_chatchart_donut(
        self,
        donut_axis,
        values: list[int],
        bar_colors: list[str],
        other_count: int,
        total_count: int,
    ) -> None:
        donut_values = list(values)
        donut_colors = list(bar_colors)
        if other_count:
            donut_values.append(other_count)
            donut_colors.append(CHATCHART_OTHER_COLOR)
        donut_labels = [""] * len(donut_values)
        if other_count:
            donut_labels[-1] = "Other"
        _wedges, outside_labels, _percentages = donut_axis.pie(
            donut_values,
            labels=donut_labels,
            colors=donut_colors,
            autopct=lambda percent: f"{percent:.0f}%" if percent >= 6 else "",
            pctdistance=0.79,
            labeldistance=1.08,
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.38, "edgecolor": "white", "linewidth": 2},
            textprops={"color": "white", "fontsize": 10, "fontweight": "bold"},
        )
        for outside_label in outside_labels:
            outside_label.set_color("#52514e")
            outside_label.set_fontweight("normal")
        donut_axis.text(
            0,
            0,
            f"{total_count:,}\nmessages",
            ha="center",
            va="center",
            fontsize=11,
        )
        donut_axis.set_title("Share by user", pad=12)
        donut_axis.axis("equal")

    def _build_chatchart_file(
        self,
        guild: discord.Guild,
        counts: list[ChannelUserCount],
        days: int,
        location_label: str,
        amount: int,
    ) -> discord.File:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise commands.UserFeedbackCheckFailure(
                "Matplotlib is required for chatchart but is not installed."
            ) from exc

        top_counts = counts[:amount]
        top_count = sum(count.message_count for count in top_counts)
        other_count = sum(count.message_count for count in counts[len(top_counts):])
        total_count = top_count + other_count
        labels: list[str] = []
        values: list[int] = []
        for count in top_counts:
            member = guild.get_member(count.user_id)
            name = member.display_name if member is not None else str(count.user_id)
            if len(name) > 32:
                name = f"{name[:29]}..."
            labels.append(name)
            values.append(count.message_count)

        bar_colors = list(CHATCHART_SERIES_COLORS[: len(values)])

        figure_height = max(5.5, 1.5 + len(top_counts) * 0.5)
        figure = plt.figure(figsize=(13, figure_height))
        grid = figure.add_gridspec(1, 2, width_ratios=(3, 1.35), wspace=0.02)
        ranking_axis = figure.add_subplot(grid[0, 0])
        donut_axis = figure.add_subplot(grid[0, 1])

        positions = list(range(len(values)))
        ranking_axis.barh(positions, values, color=bar_colors, height=0.68)
        ranking_axis.set_yticks(positions, labels=labels)
        ranking_axis.invert_yaxis()
        ranking_axis.xaxis.set_visible(False)
        ranking_axis.tick_params(axis="y", length=0)
        for spine in ranking_axis.spines.values():
            spine.set_visible(False)

        largest_value = max(values)
        ranking_axis.set_xlim(0, largest_value * 1.24)
        for position, value in zip(positions, values, strict=True):
            percentage = value / total_count * 100
            ranking_axis.text(
                value + largest_value * 0.025,
                position,
                f"{value:,} · {percentage:.1f}%",
                va="center",
                fontsize=9,
            )

        self._draw_chatchart_donut(
            donut_axis, values, bar_colors, other_count, total_count
        )

        title_y = 0.97
        figure.suptitle(
            f"Messages by user - last {days} days",
            fontsize=16,
            y=title_y,
            va="center",
        )
        figure.text(
            0.008,
            title_y,
            location_label,
            ha="left",
            va="center",
            fontsize=12,
            color="#52514e",
        )
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", bbox_inches="tight", dpi=160)
        plt.close(figure)
        buffer.seek(0)
        return discord.File(buffer, filename="chatchart.png")
