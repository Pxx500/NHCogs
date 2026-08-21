from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Any, Literal

import discord
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .catalog import CustomCommand, CustomCommandCatalog
from .lifecycle import CutoverController
from .migration import (
    LEGACY_CONFIG_IDENTIFIER,
    LegacyMigrationPlanner,
    MigrationPlan,
    command_digest,
    redact_custom_command_user_data,
)
from .migration_state import (
    MigrationApplyError,
    MigrationPhase,
    MigrationState,
    MigrationStateStore,
)

log = logging.getLogger("red.NHCogs.CustomCommands.Migration")
APPLY_CONFIRMATION = "confirm"


class CustomCommandsMigration(commands.Cog):
    """Temporary migration-only owner used before Custom Commands cutover."""

    def __init__(
        self,
        bot: Any,
        nhmisc: Any,
        catalog: CustomCommandCatalog,
        state_store: MigrationStateStore,
    ):
        super().__init__()
        self.bot = bot
        self.nhmisc = nhmisc
        self.catalog = catalog
        self.state_store = state_store
        self.planner = LegacyMigrationPlanner()
        self.controller = CutoverController(bot, nhmisc, catalog, state_store)
        self._apply_lock = asyncio.Lock()
        self._legacy_config = Config.get_conf(
            None,
            identifier=LEGACY_CONFIG_IDENTIFIER,
            cog_name="CustomCommands",
        )

    async def cog_load(self) -> None:
        await self.catalog.initialize()
        await self.state_store.initialize()

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ) -> None:
        if requester != "discord_deleted_user":
            return
        await redact_custom_command_user_data(
            self.catalog,
            self._legacy_config,
            cog_data_path(raw_name="CustomCommands") / "migration",
            user_id,
        )

    async def cog_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        expected_types = tuple(
            error_type
            for name in (
                "UserFeedbackCheckFailure",
                "CheckFailure",
                "BadArgument",
                "MissingRequiredArgument",
            )
            if isinstance((error_type := getattr(commands, name, None)), type)
        )
        original = getattr(error, "original", error)
        if isinstance(error, expected_types) or isinstance(original, expected_types):
            return
        if ctx.guild is None:
            return
        await self.nhmisc.report_operational_error(
            guild_id=ctx.guild.id,
            source="CustomCommands",
            action="legacy migration command",
            error=original,
            channel_id=getattr(ctx.channel, "id", None),
            message_id=getattr(ctx.message, "id", None),
        )

    @commands.group(name="nhcustomcom", hidden=True, invoke_without_command=True)
    @commands.guild_only()
    @commands.mod_or_permissions(manage_messages=True)
    async def nhcustomcom(self, ctx: commands.Context) -> None:
        """Run the one-time official CustomCom migration."""
        state = await self.state_store.get()
        await ctx.send(f"Custom Commands migration state: {state.phase.value}")

    @nhcustomcom.group(name="migrate", hidden=True, invoke_without_command=True)
    async def nhcustomcom_migrate(self, ctx: commands.Context) -> None:
        """Plan or apply the one-time migration."""
        await self.nhcustomcom(ctx)

    @nhcustomcom_migrate.command(name="plan", hidden=True)
    async def nhcustomcom_migrate_plan(self, ctx: commands.Context) -> None:
        """Validate legacy data and upload a complete migration plan."""
        await self._require_private_migration_context(ctx)
        state = await self.state_store.get()
        if state.phase is MigrationPhase.COMPLETE:
            raise commands.UserFeedbackCheckFailure("Migration is already complete")
        if state.phase is MigrationPhase.IMPORTED_NOT_ACTIVE:
            raise commands.UserFeedbackCheckFailure(
                "The import is already verified. Run apply confirm to retry cutover."
            )
        plan = await self._build_plan()
        artifact_directory = await asyncio.to_thread(self._write_artifacts, plan)
        await self.state_store.save(
            MigrationPhase.PLANNED,
            source_digest=plan.source_digest,
            destination_digest=plan.destination_digest,
        )
        await self._send_plan(ctx, plan, artifact_directory)

    @nhcustomcom_migrate.command(name="apply", hidden=True)
    async def nhcustomcom_migrate_apply(
        self,
        ctx: commands.Context,
        confirmation: str,
    ) -> None:
        """Import the reviewed plan and cut over to the replacement."""
        await self._require_private_migration_context(ctx)
        if confirmation.casefold() != APPLY_CONFIRMATION:
            raise commands.UserFeedbackCheckFailure(
                f"Run this command with `{APPLY_CONFIRMATION}` after reviewing the plan"
            )
        async with self._apply_lock:
            await self._apply_confirmed(ctx)

    @nhcustomcom_migrate.command(name="forgetguild", hidden=True)
    async def nhcustomcom_migrate_forgetguild(
        self,
        ctx: commands.Context,
        guild_id: int,
        confirmation: str,
    ) -> None:
        """Delete legacy CustomCom data for a guild the bot has left."""
        await self._require_private_migration_context(ctx)
        if confirmation.casefold() != APPLY_CONFIRMATION:
            raise commands.UserFeedbackCheckFailure(
                f"Run this command with `{APPLY_CONFIRMATION}` after reviewing the plan"
            )
        async with self._apply_lock:
            state = await self.state_store.get()
            if state.phase is not MigrationPhase.PLANNED:
                raise commands.UserFeedbackCheckFailure(
                    "Run the migration plan before forgetting an orphaned guild"
                )
            if self.bot.get_guild(guild_id) is not None:
                raise commands.UserFeedbackCheckFailure(
                    "The bot is still connected to that guild"
                )
            legacy_guilds = await self._legacy_config.all_guilds()
            guild_data = legacy_guilds.get(guild_id)
            commands_data = (
                guild_data.get("commands") if isinstance(guild_data, dict) else None
            )
            active_count = (
                sum(bool(record) for record in commands_data.values())
                if isinstance(commands_data, dict)
                else 0
            )
            if active_count == 0:
                raise commands.UserFeedbackCheckFailure(
                    "That orphaned guild has no active legacy CustomCom commands"
                )
            await self.state_store.save(
                MigrationPhase.NOT_PLANNED,
                source_digest=None,
                destination_digest=None,
            )
            await self._legacy_config.guild_from_id(guild_id).clear()
        noun = "command" if active_count == 1 else "commands"
        await ctx.send(
            f"Forgot {active_count} legacy CustomCom {noun} for orphaned guild "
            f"`{guild_id}`. Run `{ctx.clean_prefix}nhcustomcom migrate plan` again "
            "before apply."
        )

    async def _apply_confirmed(self, ctx: commands.Context) -> None:
        state = await self.state_store.get()
        if state.phase is MigrationPhase.COMPLETE:
            raise commands.UserFeedbackCheckFailure("Migration is already complete")
        quiesced = False
        try:
            if state.phase is MigrationPhase.PLANNED:
                await self.controller.quiesce_official()
                quiesced = True
                state = await self._import_planned(state)
            if state.phase is not MigrationPhase.IMPORTED_NOT_ACTIVE:
                raise commands.UserFeedbackCheckFailure(
                    "Run the migration plan before applying it"
                )
            await self.controller.activate_imported()
        except commands.UserFeedbackCheckFailure:
            if quiesced:
                await self.controller.restore_official()
            raise
        except Exception as error:
            latest = await self.state_store.get()
            if latest.phase is not MigrationPhase.COMPLETE:
                await self.controller.restore_official()
            await self.nhmisc.report_operational_error(
                guild_id=ctx.guild.id,
                source="CustomCommands",
                action="apply legacy migration",
                error=error,
                channel_id=ctx.channel.id,
                message_id=ctx.message.id,
            )
            raise commands.UserFeedbackCheckFailure(
                "Migration failed. Review the private operational error alert."
            ) from error
        await ctx.send("Custom Commands migration completed.")
        try:
            await self.bot.remove_cog(self.qualified_name)
        except Exception as error:
            await self.nhmisc.report_operational_error(
                guild_id=ctx.guild.id,
                source="CustomCommands",
                action="remove completed migration command",
                error=error,
                channel_id=ctx.channel.id,
                message_id=ctx.message.id,
            )

    async def _build_plan(self) -> MigrationPlan:
        legacy = await self._legacy_config.all_guilds()
        return self.planner.plan(
            legacy,
            reserved_names=self.bot.all_commands,
        )

    async def _import_planned(self, state: MigrationState) -> MigrationState:
        plan = await self._build_plan()
        if plan.source_digest != state.source_digest:
            raise commands.UserFeedbackCheckFailure(
                "Legacy CustomCom data changed after the plan. Run plan again."
            )
        if not plan.can_apply:
            raise commands.UserFeedbackCheckFailure(
                "The migration plan contains validation errors"
            )
        await self.catalog.import_migration(
            plan.commands,
            source_digest=plan.source_digest,
            destination_digest=plan.destination_digest,
        )
        guild_ids = sorted({command.guild_id for command in plan.commands})
        stored_commands: list[CustomCommand] = []
        for guild_id in guild_ids:
            stored_commands.extend(await self.catalog.list_commands(guild_id))
        stored = tuple(stored_commands)
        destination_digest = command_digest(stored)
        if destination_digest != plan.destination_digest:
            raise MigrationApplyError("Destination digest differs from the reviewed plan")
        imported_state = await self.state_store.get()
        if imported_state.phase is not MigrationPhase.IMPORTED_NOT_ACTIVE:
            raise MigrationApplyError("Verified import state was not persisted")
        return imported_state

    async def _require_private_migration_context(self, ctx: commands.Context) -> None:
        if ctx.channel.permissions_for(ctx.guild.default_role).view_channel:
            raise commands.UserFeedbackCheckFailure(
                "Run migration in a channel hidden from @everyone"
            )
        await self.nhmisc.require_private_error_channel(ctx.guild)

    @staticmethod
    def _write_artifacts(plan: MigrationPlan) -> Path:
        root = (
            cog_data_path(raw_name="CustomCommands")
            / "migration"
            / plan.source_digest[:12]
        )
        root.mkdir(parents=True, exist_ok=True)
        (root / "legacy-backup.json").write_bytes(plan.backup_json)
        (root / "migration-report.json").write_bytes(plan.report_json)
        if plan.errors_text is not None:
            (root / "migration-errors.txt").write_bytes(plan.errors_text)
        return root

    @staticmethod
    async def _send_plan(
        ctx: commands.Context,
        plan: MigrationPlan,
        artifact_directory: Path,
    ) -> None:
        summary = plan.summary
        embed = discord.Embed(
            title="Custom Commands migration plan",
            description=(
                f"Legacy commands: {summary['legacy_records']}\n"
                f"Commands ready: {summary['commands_ready']}\n"
                f"Simple: {summary['simple_commands']}\n"
                f"Random: {summary['random_commands']}\n"
                f"Responses: {summary['responses']}\n"
                f"Author metadata: {summary['authors_with_metadata']}\n"
                f"Editor IDs: {summary['editor_ids']}\n"
                f"Name conflicts: {summary['name_conflicts']}\n"
                f"Empty responses: {summary['empty_responses']}\n"
                f"Oversized responses: {summary['oversized_responses']}\n"
                f"Validation errors: {summary['issues']}\n"
                f"Source SHA-256: `{plan.source_digest}`\n"
                f"Destination SHA-256: `{plan.destination_digest}`"
            ),
        )
        files = [
            discord.File(
                io.BytesIO(plan.backup_json),
                filename="legacy-custom-commands-backup.json",
            ),
            discord.File(
                io.BytesIO(plan.report_json),
                filename="custom-commands-migration-report.json",
            ),
        ]
        if plan.errors_text is not None:
            files.append(
                discord.File(
                    io.BytesIO(plan.errors_text),
                    filename="custom-commands-migration-errors.txt",
                )
            )
        await ctx.send(
            embed=embed,
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log.info("Custom Commands migration artifacts written to %s", artifact_directory)


async def build_custom_commands_component(bot: Any, nhmisc: Any):
    database_path = (
        cog_data_path(raw_name="CustomCommands") / "custom_commands.sqlite"
    )
    catalog = CustomCommandCatalog(database_path)
    state_store = MigrationStateStore(database_path)
    await catalog.initialize()
    await state_store.initialize()
    state = await state_store.get()
    if state.phase is MigrationPhase.COMPLETE:
        controller = CutoverController(bot, nhmisc, catalog, state_store)
        try:
            return await controller.activate_completed()
        except Exception as error:
            log.exception("Custom Commands startup invariant check failed")
            guilds = tuple(bot.guilds)
            if guilds:
                await nhmisc.report_operational_error(
                    guild_id=guilds[0].id,
                    source="CustomCommands",
                    action="verify replacement startup",
                    error=error,
                )
            return CustomCommandsMigration(bot, nhmisc, catalog, state_store)
    return CustomCommandsMigration(bot, nhmisc, catalog, state_store)
