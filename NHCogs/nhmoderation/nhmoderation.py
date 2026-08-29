from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from redbot.core import commands, modlog
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from ..command_overview import channel_is_private, send_group_overview
from ..operational_errors import (
    mark_operational_error_recovered,
    report_operational_error,
)
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
AUDIT_BATCH_SIZE = 100


class NHModeration(commands.Cog):
    """Store moderation history and render moderator charts."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        database_path = cog_data_path(self) / "moderation.sqlite"
        self.history = NHModerationHistory(database_path)
        self._synchronizer: ModerationSynchronizer | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._gateway_catchup_task: asyncio.Task[None] | None = None
        self._sync_tasks: dict[int, asyncio.Task[Any]] = {}

    async def cog_load(self) -> None:
        await self.history.initialize()
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
                self._gateway_catchup_task,
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
        self._gateway_catchup_task = None
        self._sync_tasks.clear()

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        del requester
        await self.history.delete_user_data(user_id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.history.delete_guild_data(guild.id)

    async def report_operational_error(
        self,
        *,
        guild_id: int,
        action: str,
        error: BaseException,
        channel_id: int | None = None,
        message_id: int | None = None,
    ):
        log.error(
            "NHModeration operational error during %s for guild %s",
            action,
            guild_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return await report_operational_error(
            self.bot,
            guild_id=guild_id,
            source="NHModeration",
            action=action,
            error=error,
            channel_id=channel_id,
            message_id=message_id,
        )

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        red_handled_types = tuple(
            error_type
            for name in (
                "UserFeedbackCheckFailure",
                "UserInputError",
                "CommandOnCooldown",
                "DisabledCommand",
                "MaxConcurrencyReached",
                "NoPrivateMessage",
                "PrivateMessageOnly",
                "NSFWChannelRequired",
                "BotMissingPermissions",
            )
            if isinstance((error_type := getattr(commands, name, None)), type)
            and error_type not in {BaseException, Exception, object}
        )
        original = getattr(error, "original", error)
        if isinstance(error, red_handled_types) or isinstance(
            original, red_handled_types
        ):
            await ctx.bot.on_command_error(
                ctx,
                original,
                unhandled_by_cog=True,
            )
            return
        check_failure = getattr(commands, "CheckFailure", None)
        if isinstance(check_failure, type) and (
            isinstance(error, check_failure) or isinstance(original, check_failure)
        ):
            await ctx.send(
                "You do not have permission to use this command.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
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
                "Something went wrong while running this command. The error was logged.",
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

    async def _mark_operational_recovered(
        self, guild: discord.Guild, action: str
    ) -> None:
        await mark_operational_error_recovered(
            self.bot,
            guild_id=guild.id,
            source="NHModeration",
            action=action,
        )

    async def _fetch_audit_entries(
        self,
        guild: discord.Guild,
        *,
        action: str,
        after_id: int | None,
        on_batch: Callable[[Sequence[discord.AuditLogEntry]], Awaitable[None]],
    ) -> list[discord.AuditLogEntry]:
        audit_action = (
            discord.AuditLogAction.ban
            if action == "ban"
            else discord.AuditLogAction.unban
        )
        after = discord.Object(id=after_id) if after_id is not None else None
        pending: list[discord.AuditLogEntry] = []
        async for entry in guild.audit_logs(
            limit=None,
            action=audit_action,
            after=after,
            oldest_first=True,
        ):
            pending.append(entry)
            if len(pending) >= AUDIT_BATCH_SIZE:
                await on_batch(tuple(pending))
                pending.clear()
        return pending

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
                await self._mark_operational_recovered(guild, "startup sync")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._report_background_error(guild, "startup sync", error)

    async def _debounced_gateway_catchup(self) -> None:
        await asyncio.sleep(5)
        for guild in getattr(self.bot, "guilds", ()):
            try:
                state = await self.history.status(guild.id)
                if state.migration_state != "complete":
                    continue
                await self._run_sync(guild, SyncMode.INCREMENTAL)
                await self._mark_operational_recovered(guild, "gateway catch-up")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._report_background_error(
                    guild,
                    "gateway catch-up",
                    error,
                )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        task = self._gateway_catchup_task
        if task is not None and not task.done():
            return
        self._gateway_catchup_task = asyncio.create_task(
            self._debounced_gateway_catchup(),
            name="nhmoderation-gateway-catchup",
        )

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
                    await self._mark_operational_recovered(
                        guild, "weekly reconciliation"
                    )
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
            await self._mark_operational_recovered(guild, "member ban event")
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
            await self._mark_operational_recovered(guild, "member unban event")
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
            await self._mark_operational_recovered(guild, "audit event")
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
            await self._mark_operational_recovered(guild, "modlog event")
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
    @commands.has_permissions(manage_messages=True)
    async def banchart(self, ctx: commands.Context, *, arguments: str = "") -> None:
        """Render bans by credited moderator from local history."""
        state = await self.history.status(ctx.guild.id)
        if state.migration_state != "complete":
            message = (
                "Initial migration is currently running. Try banchart again after it completes."
                if state.migration_state == "running"
                else f"Run `{ctx.clean_prefix}nhmod migrate run` before using banchart."
            )
            await ctx.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
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
            await self._mark_operational_recovered(ctx.guild, "banchart")
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
        await self._mark_operational_recovered(ctx.guild, "banchart")

    @commands.group(name="nhmod", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def nhmod(self, ctx: commands.Context) -> None:
        """Manage NHModeration history and synchronization."""
        self._require_private_channel(ctx)
        await send_group_overview(ctx, include_descendants=False)
        await self._mark_operational_recovered(ctx.guild, "nhmod")

    @nhmod.command(name="status")
    async def nhmod_status(self, ctx: commands.Context) -> None:
        """Show migration and synchronization health."""
        self._require_private_channel(ctx)
        state = await self.history.status(ctx.guild.id)
        next_run = next_weekly_reconciliation(datetime.now(timezone.utc))
        running = ctx.guild.id in self._sync_tasks
        embed = discord.Embed(title="NHModeration status")
        embed.add_field(name="Migration", value=state.migration_state, inline=False)
        embed.add_field(
            name="Historical coverage gap",
            value="possible" if state.historical_gap else "none detected",
            inline=False,
        )
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
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await self._mark_operational_recovered(ctx.guild, "nhmod status")

    @nhmod.group(name="migrate", invoke_without_command=True)
    async def nhmod_migrate(self, ctx: commands.Context) -> None:
        """Plan or run the initial moderation history import."""
        self._require_private_channel(ctx)
        await send_group_overview(ctx)
        await self._mark_operational_recovered(ctx.guild, "nhmod migrate")

    @nhmod_migrate.command(name="plan")
    async def nhmod_migrate_plan(self, ctx: commands.Context) -> None:
        """Check readiness without importing Discord history."""
        self._require_private_channel(ctx)
        permissions = ctx.guild.me.guild_permissions
        state = await self.history.status(ctx.guild.id)
        command = self.bot.get_command("banchart")
        owner = getattr(command, "cog", None)
        if command is None:
            command_status = "not registered"
        elif owner is self:
            command_status = "ready"
        else:
            owner_name = getattr(owner, "qualified_name", None) or type(owner).__name__
            command_status = f"conflict with {owner_name}"
        lines = [
            f"Migration: {state.migration_state}",
            "Database: ready",
            (
                "Discord audit history: ready"
                if permissions.view_audit_log
                else "Discord audit history: missing View Audit Log"
            ),
            (
                "Active ban snapshot: ready"
                if permissions.ban_members
                else "Active ban snapshot: missing Ban Members"
            ),
            (
                "Red ModLog: ready"
                if callable(getattr(modlog, "get_all_cases", None))
                else "Red ModLog: unavailable"
            ),
            f"BanChart command: {command_status}",
        ]
        await ctx.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())
        await self._mark_operational_recovered(ctx.guild, "nhmod migrate plan")

    @nhmod_migrate.command(name="run")
    async def nhmod_migrate_run(self, ctx: commands.Context) -> None:
        """Start or resume the initial moderation history import."""
        self._require_private_channel(ctx)
        state = await self.history.status(ctx.guild.id)
        if state.migration_state == "complete":
            await ctx.send(
                "Initial migration is already complete.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self._mark_operational_recovered(ctx.guild, "nhmod migrate run")
            return
        await ctx.send(
            "Migration started. I will post the result here when it completes.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log.info("NHModeration initial migration started for guild %s", ctx.guild.id)
        report = await self._run_sync(ctx.guild, SyncMode.INITIAL)
        log.info(
            "NHModeration initial migration completed for guild %s with %s new observations",
            ctx.guild.id,
            report.inserted_observations,
        )
        await ctx.send(
            f"Migration complete. Imported {report.inserted_observations} new observations.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._mark_operational_recovered(ctx.guild, "nhmod migrate run")

    @nhmod.command(name="sync")
    async def nhmod_sync(self, ctx: commands.Context) -> None:
        """Run a low-cost incremental synchronization."""
        self._require_private_channel(ctx)
        report = await self._run_sync(ctx.guild, SyncMode.INCREMENTAL)
        await ctx.send(
            f"Synchronization complete. Imported {report.inserted_observations} new observations.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._mark_operational_recovered(ctx.guild, "nhmod sync")

    @nhmod.command(name="repair", usage="[confirm]")
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
        await self._mark_operational_recovered(ctx.guild, "nhmod repair")
