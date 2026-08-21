from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from redbot import VersionInfo
from redbot.core import commands, version_info
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from .controller import MigrationApplyError, MigrationController
from .plan import MigrationPreflightPlan, build_preflight_plan
from .red_runtime import RedRuntime
from .state import MigrationRun, MigrationState, MigrationStateStore

log = logging.getLogger("red.NHCogsMigrator")
_CONFIRMATION_DELAY = timedelta(seconds=10)
_RECOVERABLE_PRECOMMIT = {
    MigrationState.QUIESCING,
    MigrationState.BACKUP_COMPLETE,
    MigrationState.LOADING_SUITE,
    MigrationState.VALIDATED,
    MigrationState.ROLLING_BACK,
}
_MINIMUM_RED_VERSION = VersionInfo.from_str("3.5.23")


class NHCogsMigrator(commands.Cog):
    """Temporary verified migration helper for the NHCogs consolidation."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._runtime = RedRuntime(bot)
        data_path = cog_data_path(self)
        self._store = MigrationStateStore(data_path / "migration.sqlite")
        self._backup_root = data_path / "backups"
        process_token = getattr(bot, "_nhcogs_migration_process_token", None)
        if process_token is None:
            process_token = uuid.uuid4().hex
            bot._nhcogs_migration_process_token = process_token
        self._process_token = str(process_token)
        self._controller = MigrationController(
            self._runtime,
            self._store,
            self._backup_root,
            process_token=self._process_token,
        )
        self._active_task: asyncio.Task[Any] | None = None
        self._recovery_task: asyncio.Task[Any] | None = None

    async def cog_load(self) -> None:
        if version_info < _MINIMUM_RED_VERSION:
            raise RuntimeError("NHCogsMigrator requires Red 3.5.23 or newer")
        await self._store.initialize()
        self._recovery_task = asyncio.create_task(self._recover_after_ready())

    async def cog_unload(self) -> None:
        recovery = self._recovery_task
        if recovery is not None and recovery is not asyncio.current_task():
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        active = self._active_task
        if active is not None and active is not asyncio.current_task():
            await asyncio.shield(active)

    @commands.group(
        name="nhcogsmigrate",
        hidden=True,
        invoke_without_command=True,
    )
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def nhcogsmigrate(self, ctx: commands.Context) -> None:
        """Run the temporary NHCogs suite migration."""
        await self._require_private_channel(ctx)
        await self._send_status(ctx)

    @nhcogsmigrate.command(name="plan", hidden=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def nhcogsmigrate_plan(self, ctx: commands.Context) -> None:
        """Create a read-only migration plan."""
        await self._require_private_channel(ctx)
        existing = await self._store.latest_run()
        if existing is not None and existing.state is not MigrationState.ROLLED_BACK:
            raise commands.UserFeedbackCheckFailure(
                f"Migration run `{existing.run_id}` already exists in state "
                f"`{existing.state.value}`."
            )
        plan = await build_preflight_plan(
            self._runtime,
            backup_root=self._backup_root,
        )
        run_id = uuid.uuid4().hex
        report = _plan_report(run_id, plan)
        report_bytes = json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        if plan.blocking_issues:
            await ctx.send(
                embed=_plan_embed(run_id, plan, ctx.clean_prefix),
                file=discord.File(
                    fp=io.BytesIO(report_bytes),
                    filename=f"nhcogs-migration-{run_id}-plan.json",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        report_path = await asyncio.to_thread(
            _store_plan_report,
            cog_data_path(self) / "plans",
            run_id,
            report_bytes,
        )
        report_hash = hashlib.sha256(report_bytes).hexdigest()
        not_before = datetime.now(timezone.utc) + _CONFIRMATION_DELAY
        validations = plan.validations()
        validations.update(
            {
                "plan_guild_id": int(ctx.guild.id),
                "plan_channel_id": int(ctx.channel.id),
                "confirmation_not_before": not_before.isoformat(),
            }
        )
        await self._store.create_run(
            run_id,
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=validations,
            artifacts={"plan_report": str(report_path)},
            checksums={"plan_report": report_hash},
        )
        await ctx.send(
            embed=_plan_embed(run_id, plan, ctx.clean_prefix),
            file=discord.File(
                fp=io.BytesIO(report_bytes),
                filename=f"nhcogs-migration-{run_id}-plan.json",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhcogsmigrate.command(name="status", hidden=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def nhcogsmigrate_status(self, ctx: commands.Context) -> None:
        """Show the durable migration state."""
        await self._require_private_channel(ctx)
        await self._send_status(ctx)

    @nhcogsmigrate.command(name="apply", hidden=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def nhcogsmigrate_apply(
        self,
        ctx: commands.Context,
        confirm: str,
    ) -> None:
        """Apply the reviewed plan with a typed confirm."""
        await self._require_private_channel(ctx)
        if confirm.casefold() != "confirm":
            raise commands.UserFeedbackCheckFailure(
                "Type `confirm` after the command to apply the migration."
            )
        run = await self._store.latest_run()
        if run is None or run.state is not MigrationState.PLANNED:
            raise commands.UserFeedbackCheckFailure(
                "There is no planned migration ready to apply."
            )
        not_before = datetime.fromisoformat(
            str(run.validations["confirmation_not_before"])
        )
        remaining = (not_before - datetime.now(timezone.utc)).total_seconds()
        if remaining > 0:
            raise commands.UserFeedbackCheckFailure(
                f"Review the plan for another {max(1, int(remaining + 0.999))} seconds."
            )
        plan = await build_preflight_plan(
            self._runtime,
            backup_root=self._backup_root,
        )
        if not _plan_matches_run(plan, run):
            raise commands.UserFeedbackCheckFailure(
                "The migration preflight changed. Run a new plan before applying."
            )
        self._active_task = asyncio.current_task()
        try:
            result = await self._controller.apply(run.run_id, plan)
        except MigrationApplyError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        finally:
            self._active_task = None
        backup_path = Path(str(result.artifacts["backup_path"]))
        compact_artifacts = (
            backup_path / "manifest.json",
            backup_path / "manifest.sha256",
            backup_path / "metadata.json",
            backup_path / "config" / "NHMisc.json",
            backup_path / "config" / "Honeypot.json",
        )
        await ctx.send(
            f"Migration `{result.run_id}` committed. Restart the bot normally, "
            f"then run `{ctx.clean_prefix}nhcogsmigrate status`.",
            files=[discord.File(path) for path in compact_artifacts],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhcogsmigrate.command(name="finalize", hidden=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def nhcogsmigrate_finalize(self, ctx: commands.Context) -> None:
        """Finalize after one verified normal restart."""
        await self._require_private_channel(ctx)
        run = await self._store.latest_run()
        if run is None:
            raise commands.UserFeedbackCheckFailure("There is no migration run.")
        try:
            result = await self._controller.finalize(run.run_id)
        except MigrationApplyError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        extension_key = self._runtime.extension_key_for_cog("NHCogsMigrator")
        await ctx.send(
            f"Migration `{result.run_id}` finalized. Verified backups remain at "
            f"`{result.artifacts.get('backup_path', 'unknown')}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        asyncio.create_task(self._deferred_self_unload(extension_key))

    @nhcogsmigrate.command(name="recover", hidden=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def nhcogsmigrate_recover(
        self,
        ctx: commands.Context,
        confirm: str,
    ) -> None:
        """Retry verified rollback after manual intervention."""
        await self._require_private_channel(ctx)
        if confirm.casefold() != "confirm":
            raise commands.UserFeedbackCheckFailure(
                "Type `confirm` after the command to retry recovery."
            )
        run = await self._store.latest_run()
        if run is None or run.state is not MigrationState.MANUAL_INTERVENTION:
            raise commands.UserFeedbackCheckFailure(
                "There is no manual-intervention recovery to retry."
            )
        self._active_task = asyncio.current_task()
        try:
            result = await self._controller.recover_interrupted(run.run_id)
        except MigrationApplyError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        finally:
            self._active_task = None
        await ctx.send(
            f"Migration `{result.run_id}` recovered to `{result.state.value}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_status(self, ctx: commands.Context) -> None:
        run = await self._store.latest_run()
        if run is None:
            await ctx.send(
                "No NHCogs migration run exists. Use `nhcogsmigrate plan`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if (
            run.state is MigrationState.COMMITTED
            and run.validations.get("committed_process") != self._process_token
        ):
            try:
                run = await self._controller.verify_restart(run.run_id)
            except MigrationApplyError as error:
                raise commands.UserFeedbackCheckFailure(str(error)) from error
        await ctx.send(
            embed=_status_embed(run),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _recover_after_ready(self) -> None:
        await self.bot.wait_until_red_ready()
        run = await self._store.latest_run()
        if run is None:
            return
        try:
            if run.state in _RECOVERABLE_PRECOMMIT:
                run = await self._controller.recover_interrupted(run.run_id)
            elif (
                run.state is MigrationState.COMMITTED
                and run.validations.get("committed_process") != self._process_token
            ):
                run = await self._controller.verify_restart(run.run_id)
            elif run.state is MigrationState.FINALIZED:
                run = await self._controller.finalize(run.run_id)
                await self._send_recovery_report(run)
                extension_key = self._runtime.extension_key_for_cog(
                    "NHCogsMigrator"
                )
                asyncio.create_task(self._deferred_self_unload(extension_key))
                return
            else:
                return
            await self._send_recovery_report(run)
        except Exception:
            log.exception("NHCogs migration startup recovery failed")

    async def _send_recovery_report(self, run: MigrationRun) -> None:
        channel_id = run.validations.get("plan_channel_id")
        if not isinstance(channel_id, int):
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        everyone_permissions = channel.permissions_for(channel.guild.default_role)
        if everyone_permissions.view_channel:
            log.error(
                "NHCogs migration recovery report was withheld because channel %s "
                "is visible to @everyone",
                channel_id,
            )
            return
        await channel.send(
            embed=_status_embed(run),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _deferred_self_unload(self, extension_key: str) -> None:
        await asyncio.sleep(0)
        await self._runtime.unload_extension(extension_key)

    @staticmethod
    async def _require_private_channel(ctx: commands.Context) -> None:
        author_permissions = ctx.channel.permissions_for(ctx.author)
        if not author_permissions.manage_messages:
            raise commands.UserFeedbackCheckFailure(
                "You need the Discord Manage Messages permission to run this command."
            )
        permissions = ctx.channel.permissions_for(ctx.guild.default_role)
        if permissions.view_channel:
            raise commands.UserFeedbackCheckFailure(
                "Run NHCogs migration commands in a channel hidden from @everyone."
            )


def _plan_matches_run(plan: MigrationPreflightPlan, run: MigrationRun) -> bool:
    stored_inventory = run.validations.get("inventory")
    stored_paths = run.validations.get("data_directories")
    stored_dependencies = run.validations.get("dependency_versions")
    stored_commits = run.validations.get("installed_commits")
    return (
        not plan.blocking_issues
        and plan.original_packages == run.original_packages
        and plan.source_commit == run.source_commit
        and plan.inventory.as_dict() == stored_inventory
        and plan.validations()["data_directories"] == stored_paths
        and plan.dependency_versions == stored_dependencies
        and plan.installed_commits == stored_commits
    )


def _plan_report(run_id: str, plan: MigrationPreflightPlan) -> dict[str, object]:
    return {
        "run_id": run_id,
        "original_packages": list(plan.original_packages),
        "source_commit": plan.source_commit,
        **plan.validations(),
    }


def _plan_embed(
    run_id: str,
    plan: MigrationPreflightPlan,
    prefix: str,
) -> discord.Embed:
    ready = not plan.blocking_issues
    embed = discord.Embed(
        title="NHCogs migration plan",
        description=f"Run ID: `{run_id}`\nStatus: {'ready' if ready else 'blocked'}",
        color=discord.Color.green() if ready else discord.Color.red(),
    )
    embed.add_field(
        name="Persisted data",
        value=(
            f"{plan.persisted_data.file_count} files, "
            f"{plan.persisted_data.database_count} SQLite databases, "
            f"{plan.persisted_data.total_bytes} bytes"
        ),
        inline=False,
    )
    embed.add_field(
        name="Config",
        value=", ".join(
            f"{name}: {count} guilds"
            for name, count in plan.config_guild_counts.items()
        ),
        inline=False,
    )
    if plan.blocking_issues:
        embed.add_field(
            name="Blocking issues",
            value="\n".join(f"• {issue}" for issue in plan.blocking_issues)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Next step",
            value=(
                "After reviewing the attachment, run "
                f"`{prefix}nhcogsmigrate apply confirm` for `{run_id}`."
            ),
            inline=False,
        )
    return embed


def _status_embed(run: MigrationRun) -> discord.Embed:
    embed = discord.Embed(
        title="NHCogs migration status",
        description=f"Run ID: `{run.run_id}`\nState: `{run.state.value}`",
        color=discord.Color.blue(),
    )
    backup_path = run.artifacts.get("backup_path")
    if backup_path is not None:
        embed.add_field(name="Verified backup", value=f"`{backup_path}`", inline=False)
    return embed


def _store_plan_report(
    root: Path,
    run_id: str,
    report: bytes,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{run_id}.json"
    with path.open("xb") as file:
        file.write(report)
        file.flush()
        os.fsync(file.fileno())
    return path.resolve()
