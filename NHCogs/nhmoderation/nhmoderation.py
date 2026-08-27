from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from redbot.core import Config, commands, modlog
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from ..command_overview import channel_is_private, send_group_overview
from ..operational_errors import OperationalErrorReporter, OperationalFailure
from ..ranked_donut_chart import render_ranked_donut_chart
from .command_inputs import parse_banchart_arguments
from .history import NHModerationHistory
from .models import BanChartQuery, ModerationObservation
from .synchronization import (
    ModerationSynchronizer,
    SyncMode,
    audit_observation,
    modlog_observation,
    next_weekly_reconciliation,
)

log = logging.getLogger("red.NHModeration")


class NHModeration(commands.Cog):
    """Store moderation history and render moderator charts."""

    CONFIG_IDENTIFIER = 205192943327321000143939875896557571751

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=self.CONFIG_IDENTIFIER,
            force_registration=True,
        )
        self.config.register_guild(error_channel=None, error_maintainer_id=None)
        database_path = cog_data_path(self) / "moderation.sqlite"
        self.history = NHModerationHistory(database_path)
        self._operational_errors = OperationalErrorReporter(
            bot, self.config, database_path, logger=log
        )
        self._synchronizer: ModerationSynchronizer | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._sync_tasks: dict[int, asyncio.Task[Any]] = {}

    async def cog_load(self) -> None:
        await self.history.initialize()
        await self._operational_errors.initialize()
        self._synchronizer = ModerationSynchronizer(
            self.history,
            bot_user_id=lambda: getattr(getattr(self.bot, "user", None), "id", 0),
            audit_fetcher=self._fetch_audit_entries,
            modlog_fetcher=self._fetch_modlog_cases,
            snapshot_fetcher=self._fetch_ban_snapshot,
        )
        self._scheduler_task = asyncio.create_task(
            self._weekly_scheduler(), name="nhmoderation-weekly-reconciliation"
        )
        self._startup_task = asyncio.create_task(
            self._startup_catchup(), name="nhmoderation-startup-catchup"
        )

    async def cog_unload(self) -> None:
        tasks = [
            task
            for task in (
                self._scheduler_task,
                self._startup_task,
                *self._sync_tasks.values(),
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduler_task = None
        self._startup_task = None
        self._sync_tasks.clear()

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        del requester
        await self.history.delete_user_data(user_id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.history.delete_guild_data(guild.id)
        await self._operational_errors.delete_guild(guild.id)
        await self.config.guild(guild).clear()

    async def report_operational_error(
        self,
        *,
        guild_id: int,
        action: str,
        error: BaseException,
        channel_id: int | None = None,
        message_id: int | None = None,
    ) -> OperationalFailure | None:
        try:
            return await self._operational_errors.report(
                guild_id=guild_id,
                source="NHModeration",
                action=action,
                error=error,
                channel_id=channel_id,
                message_id=message_id,
            )
        except Exception:
            log.exception(
                "Failed to persist NHModeration operational error for guild %s",
                guild_id,
            )
            return None

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        expected_types = tuple(
            error_type
            for name in (
                "UserFeedbackCheckFailure",
                "UserInputError",
                "CheckFailure",
                "CommandOnCooldown",
                "DisabledCommand",
                "MaxConcurrencyReached",
            )
            if isinstance((error_type := getattr(commands, name, None)), type)
            and error_type not in {BaseException, Exception, object}
        )
        original = getattr(error, "original", error)
        if isinstance(error, expected_types) or isinstance(original, expected_types):
            return
        guild = getattr(ctx, "guild", None)
        if guild is not None:
            command = getattr(ctx, "command", None)
            await self.report_operational_error(
                guild_id=guild.id,
                action=getattr(command, "qualified_name", None) or "unknown command",
                error=original,
                channel_id=getattr(getattr(ctx, "channel", None), "id", None),
                message_id=getattr(getattr(ctx, "message", None), "id", None),
            )
        try:
            await ctx.send(
                "Something went wrong while running this command. The error was logged for the maintainer.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Failed to send NHModeration command error feedback")

    async def _report_background_error(
        self, guild: discord.Guild, action: str, error: BaseException
    ) -> None:
        await self.report_operational_error(
            guild_id=guild.id,
            action=action,
            error=error,
        )

    async def _fetch_audit_entries(
        self, guild: discord.Guild, *, action: str, after_id: int | None
    ) -> list[discord.AuditLogEntry]:
        audit_action = (
            discord.AuditLogAction.ban
            if action == "ban"
            else discord.AuditLogAction.unban
        )
        after = discord.Object(id=after_id) if after_id is not None else None
        return [
            entry
            async for entry in guild.audit_logs(
                limit=None,
                action=audit_action,
                after=after,
                oldest_first=True,
            )
        ]

    async def _fetch_modlog_cases(
        self, guild: discord.Guild, *, after_case: int | None
    ) -> list[Any]:
        cases = await modlog.get_all_cases(guild, self.bot)
        if after_case is None:
            return list(cases)
        return [case for case in cases if int(case.case_number) > after_case]

    async def _fetch_ban_snapshot(self, guild: discord.Guild) -> list[discord.BanEntry]:
        return [entry async for entry in guild.bans(limit=None)]

    async def _run_sync(self, guild: discord.Guild, mode: SyncMode):
        if self._synchronizer is None:
            raise RuntimeError("NHModeration is not initialized")
        existing = self._sync_tasks.get(guild.id)
        if existing is not None and not existing.done():
            previous = await existing
            if previous.mode is mode:
                return previous
            if self._sync_tasks.get(guild.id) is existing:
                self._sync_tasks.pop(guild.id, None)
        task = asyncio.create_task(
            self._synchronizer.synchronize(guild, mode),
            name=f"nhmoderation-{mode.value}-{guild.id}",
        )
        self._sync_tasks[guild.id] = task
        try:
            return await task
        finally:
            if self._sync_tasks.get(guild.id) is task:
                self._sync_tasks.pop(guild.id, None)

    async def _startup_catchup(self) -> None:
        await self.bot.wait_until_red_ready()
        for guild in getattr(self.bot, "guilds", ()):
            try:
                state = await self.history.status(guild.id)
                if state.migration_state != "complete":
                    continue
                await self._run_sync(guild, SyncMode.INCREMENTAL)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._report_background_error(guild, "startup sync", error)

    async def _weekly_scheduler(self) -> None:
        await self.bot.wait_until_red_ready()
        while True:
            now = datetime.now(timezone.utc)
            due = next_weekly_reconciliation(now)
            await asyncio.sleep(max(0.0, (due - now).total_seconds()))
            for guild in getattr(self.bot, "guilds", ()):
                try:
                    state = await self.history.status(guild.id)
                    if state.migration_state != "complete":
                        continue
                    await self._run_sync(guild, SyncMode.WEEKLY)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    await self._report_background_error(
                        guild, "weekly reconciliation", error
                    )

    @commands.Cog.listener()
    async def on_member_ban(
        self, guild: discord.Guild, user: discord.User | discord.Member
    ) -> None:
        now = datetime.now(timezone.utc)
        try:
            await self.history.observe(
                ModerationObservation(
                    guild_id=guild.id,
                    source_kind="discord_gateway",
                    source_key=None,
                    action_hint="ban",
                    target_user_id=user.id,
                    occurred_at=now,
                    observed_at=now,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._report_background_error(guild, "member ban event", error)

    @commands.Cog.listener()
    async def on_member_unban(
        self, guild: discord.Guild, user: discord.User | discord.Member
    ) -> None:
        now = datetime.now(timezone.utc)
        try:
            await self.history.observe(
                ModerationObservation(
                    guild_id=guild.id,
                    source_kind="discord_gateway",
                    source_key=None,
                    action_hint="unban",
                    target_user_id=user.id,
                    occurred_at=now,
                    observed_at=now,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._report_background_error(guild, "member unban event", error)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        action_name = getattr(getattr(entry, "action", None), "name", "")
        if action_name not in {"ban", "unban"}:
            return
        guild = entry.guild
        now = datetime.now(timezone.utc)
        try:
            await self.history.observe(
                audit_observation(
                    guild.id,
                    entry,
                    action_name,
                    getattr(getattr(self.bot, "user", None), "id", 0),
                    now,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._report_background_error(guild, "audit event", error)

    @commands.Cog.listener()
    async def on_modlog_case_create(self, case: Any) -> None:
        guild = getattr(case, "guild", None)
        if guild is None:
            return
        now = datetime.now(timezone.utc)
        item = modlog_observation(
            guild.id,
            case,
            getattr(getattr(self.bot, "user", None), "id", 0),
            now,
        )
        if item is None:
            return
        try:
            await self.history.observe(item)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._report_background_error(guild, "modlog event", error)

    def _require_private_channel(self, ctx: commands.Context) -> None:
        if not channel_is_private(ctx.guild, ctx.channel):
            raise commands.UserFeedbackCheckFailure(
                "Run this command in a channel hidden from @everyone"
            )

    @commands.command(
        name="banchart",
        usage="[days|all] [amount] [--automation]",
    )
    @commands.guild_only()
    @commands.mod_or_permissions(ban_members=True)
    async def banchart(self, ctx: commands.Context, *, arguments: str = "") -> None:
        """Render bans by credited moderator from local history."""
        self._require_private_channel(ctx)
        try:
            parsed = parse_banchart_arguments(arguments)
        except ValueError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        since = (
            datetime.now(timezone.utc) - timedelta(days=parsed.days)
            if parsed.days is not None
            else None
        )
        data = await self.history.get_ban_chart(
            BanChartQuery(
                guild_id=ctx.guild.id,
                since=since,
                amount=parsed.amount,
                include_automation=parsed.include_automation,
            )
        )
        if data.total_count == 0:
            await ctx.send(
                "No retained bans match this chart.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        rows = []
        for row in data.rows:
            if row.label is not None:
                label = row.label
            else:
                member = ctx.guild.get_member(row.moderator_user_id)
                label = (
                    member.display_name
                    if member is not None
                    else str(row.moderator_user_id)
                )
            rows.append((label, row.count))
        period = "all retained history" if parsed.days is None else f"last {parsed.days} days"
        try:
            chart = render_ranked_donut_chart(
                rows,
                other_count=data.other_count,
                title=f"Bans by moderator - {period}",
                context_label=ctx.guild.name,
                center_unit="bans",
                donut_title="Share by moderator",
                filename="banchart.png",
            )
        except ImportError as error:
            raise commands.UserFeedbackCheckFailure(
                "Matplotlib is required for banchart but is not installed"
            ) from error
        await ctx.send(file=chart, allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="nhmod", invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def nhmod(self, ctx: commands.Context) -> None:
        """Manage NHModeration history and synchronization."""
        await send_group_overview(ctx, include_descendants=False)

    @nhmod.command(name="status")
    async def nhmod_status(self, ctx: commands.Context) -> None:
        """Show migration and synchronization health."""
        self._require_private_channel(ctx)
        state = await self.history.status(ctx.guild.id)
        next_run = next_weekly_reconciliation(datetime.now(timezone.utc))
        running = ctx.guild.id in self._sync_tasks
        active_failures = await self._operational_errors.active_count(ctx.guild.id)
        embed = discord.Embed(title="NHModeration status")
        embed.add_field(name="Migration", value=state.migration_state, inline=False)
        embed.add_field(name="Sync running", value="yes" if running else "no", inline=False)
        embed.add_field(
            name="Last sync",
            value=state.last_sync_at.isoformat() if state.last_sync_at else "never",
            inline=False,
        )
        embed.add_field(
            name="Last weekly reconciliation",
            value=(
                state.last_reconciliation_at.isoformat()
                if state.last_reconciliation_at
                else "never"
            ),
            inline=False,
        )
        embed.add_field(name="Next weekly reconciliation", value=next_run.isoformat(), inline=False)
        embed.add_field(name="Active operational failures", value=str(active_failures), inline=False)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @nhmod.group(name="migrate", invoke_without_command=True)
    async def nhmod_migrate(self, ctx: commands.Context) -> None:
        """Plan or run the initial moderation history import."""
        await send_group_overview(ctx)

    @nhmod_migrate.command(name="plan")
    @commands.admin_or_permissions(administrator=True)
    async def nhmod_migrate_plan(self, ctx: commands.Context) -> None:
        """Check readiness without importing Discord history."""
        self._require_private_channel(ctx)
        permissions = ctx.guild.me.guild_permissions
        missing = []
        if not permissions.view_audit_log:
            missing.append("View Audit Log")
        if not permissions.ban_members:
            missing.append("Ban Members")
        state = await self.history.status(ctx.guild.id)
        lines = [f"Migration: {state.migration_state}"]
        lines.append(
            "Permissions: ready" if not missing else f"Missing permissions: {', '.join(missing)}"
        )
        lines.append("Database: ready")
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @nhmod_migrate.command(name="run")
    @commands.admin_or_permissions(administrator=True)
    async def nhmod_migrate_run(self, ctx: commands.Context) -> None:
        """Start or resume the initial moderation history import."""
        self._require_private_channel(ctx)
        report = await self._run_sync(ctx.guild, SyncMode.INITIAL)
        await ctx.send(
            f"Migration complete. Imported {report.inserted_observations} new observations.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmod.command(name="sync")
    @commands.admin_or_permissions(administrator=True)
    async def nhmod_sync(self, ctx: commands.Context) -> None:
        """Run a low-cost incremental synchronization."""
        self._require_private_channel(ctx)
        report = await self._run_sync(ctx.guild, SyncMode.INCREMENTAL)
        await ctx.send(
            f"Synchronization complete. Imported {report.inserted_observations} new observations.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmod.command(name="repair", usage="[confirm]")
    @commands.admin_or_permissions(administrator=True)
    async def nhmod_repair(
        self, ctx: commands.Context, confirmation: str | None = None
    ) -> None:
        """Re-import available sources and rebuild local history."""
        self._require_private_channel(ctx)
        if confirmation != "confirm":
            raise commands.UserFeedbackCheckFailure(
                f"Repair reads all available sources and the active ban list. Run `{ctx.clean_prefix}nhmod repair confirm` to continue"
            )
        report = await self._run_sync(ctx.guild, SyncMode.REPAIR)
        await ctx.send(
            f"Repair complete. Imported {report.inserted_observations} new observations.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmod.group(name="errors", invoke_without_command=True)
    async def nhmod_errors(self, ctx: commands.Context) -> None:
        """Configure private operational error reporting."""
        await send_group_overview(
            ctx,
            lambda: self._send_error_configuration(ctx),
        )

    async def _send_error_configuration(self, ctx: commands.Context) -> None:
        guild_config = self.config.guild(ctx.guild)
        channel_id = await guild_config.error_channel()
        maintainer_id = await guild_config.error_maintainer_id()
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        maintainer = ctx.guild.get_member(maintainer_id) if maintainer_id else None
        active = await self._operational_errors.active_count(ctx.guild.id)
        embed = discord.Embed(title="Operational errors")
        embed.add_field(
            name="Current configuration",
            value=(
                f"Channel: {channel.mention if channel else 'Not configured'}\n"
                f"Maintainer: {maintainer.mention if maintainer else 'Not configured'}\n"
                f"Active failures: {active}"
            ),
            inline=False,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @nhmod_errors.group(name="channel", invoke_without_command=True)
    async def nhmod_errors_channel(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        """Show or set the private operational error channel."""
        self._require_private_channel(ctx)
        setting = self.config.guild(ctx.guild).error_channel
        if channel is None:
            channel_id = await setting()
            current = ctx.guild.get_channel(channel_id) if channel_id else None
            await ctx.send(
                f"Operational error channel: {current.mention if current else 'Not configured'}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not channel_is_private(ctx.guild, channel):
            raise commands.UserFeedbackCheckFailure(
                "Configure a channel that is private from @everyone"
            )
        permissions = channel.permissions_for(ctx.guild.me)
        missing = [
            label
            for attribute, label in (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
                ("attach_files", "Attach Files"),
            )
            if not getattr(permissions, attribute, False)
        ]
        if missing:
            raise commands.UserFeedbackCheckFailure(
                f"The bot is missing these permissions: {', '.join(missing)}"
            )
        await setting.set(channel.id)
        await ctx.send(
            f"Operational error channel set to {channel.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmod_errors_channel.command(name="clear")
    async def nhmod_errors_channel_clear(self, ctx: commands.Context) -> None:
        """Clear the operational error channel."""
        self._require_private_channel(ctx)
        await self.config.guild(ctx.guild).error_channel.clear()
        await ctx.send("Operational error channel cleared.")

    @nhmod_errors.group(name="maintainer", invoke_without_command=True)
    async def nhmod_errors_maintainer(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show or set the operational error maintainer."""
        self._require_private_channel(ctx)
        setting = self.config.guild(ctx.guild).error_maintainer_id
        if member is not None:
            await setting.set(member.id)
            await ctx.send(
                f"Operational error maintainer set to {member.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        maintainer_id = await setting()
        maintainer = ctx.guild.get_member(maintainer_id) if maintainer_id else None
        await ctx.send(
            f"Operational error maintainer: {maintainer.mention if maintainer else 'Not configured'}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmod_errors_maintainer.command(name="clear")
    async def nhmod_errors_maintainer_clear(self, ctx: commands.Context) -> None:
        """Clear the operational error maintainer."""
        self._require_private_channel(ctx)
        await self.config.guild(ctx.guild).error_maintainer_id.clear()
        await ctx.send("Operational error maintainer cleared.")
