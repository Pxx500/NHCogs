from __future__ import annotations

import logging
from io import BytesIO
from typing import Literal

import discord
import rapidfuzz
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path
from redbot.core.utils import menus
from redbot.core.utils.chat_formatting import pagify

from .catalog import (
    CatalogError,
    CustomCommand,
    CustomCommandCatalog,
    ResponseDraft,
    StaleRevision,
)
from .migration import (
    LEGACY_CONFIG_IDENTIFIER,
    LegacyCleanupError,
    LegacyCleanupPreconditionError,
    inspect_legacy_data,
    purge_legacy_data,
    redact_custom_command_user_data,
)
from .presentation import build_response_transcript, present_exact_response
from .runtime import CustomCommandRuntime
from .workflows import WorkflowDraft, WorkflowManager

log = logging.getLogger("red.NHCogs.CustomCommands")
PREVIEW_LENGTH = 52
COMMANDS_PER_PAGE = 15
EMBED_PAGE_LENGTH = 3_800
FUZZY_MATCH_THRESHOLD = 60
INTERACTIVE_VIEW_TIMEOUT_SECONDS = 30
COMMAND_NOT_FOUND_MESSAGE = "That custom command doesn't exist"


class CommandListView(discord.ui.View):
    def __init__(
        self,
        cog: CustomCommands,
        *,
        requester_id: int,
        pages: tuple[discord.Embed, ...],
    ):
        super().__init__(timeout=INTERACTIVE_VIEW_TIMEOUT_SECONDS)
        self._cog = cog
        self._requester_id = requester_id
        self._pages = pages
        self._page = 0
        self.message: discord.Message | None = None
        self._previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
        )
        self._close_button = discord.ui.Button(
            label="X",
            style=discord.ButtonStyle.danger,
        )
        self._next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
        )
        self._previous_button.callback = self._previous
        self._close_button.callback = self._close
        self._next_button.callback = self._next
        self.add_item(self._previous_button)
        self.add_item(self._close_button)
        self.add_item(self._next_button)
        self._update_navigation_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this command can use these controls.",
            ephemeral=True,
        )
        return False

    async def _previous(self, interaction: discord.Interaction) -> None:
        self._page -= 1
        self._update_navigation_buttons()
        await interaction.response.edit_message(
            embed=self._pages[self._page],
            view=self,
        )

    async def _close(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self.message is not None:
            await self.message.delete()
        self.stop()

    async def _next(self, interaction: discord.Interaction) -> None:
        self._page += 1
        self._update_navigation_buttons()
        await interaction.response.edit_message(
            embed=self._pages[self._page],
            view=self,
        )

    def _update_navigation_buttons(self) -> None:
        self._previous_button.disabled = self._page == 0
        self._next_button.disabled = self._page == len(self._pages) - 1

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception as error:
                await self._cog._report_view_timeout_error(
                    self.message,
                    action="expire custom command list",
                    error=error,
                )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[CommandListView],
    ) -> None:
        if interaction.guild is not None:
            await self._cog.nhmisc.report_operational_error(
                guild_id=interaction.guild.id,
                source="CustomCommands",
                action="browse custom command list",
                error=error,
                channel_id=getattr(interaction.channel, "id", None),
                message_id=getattr(self.message, "id", None),
            )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Could not update the command list. The error was reported",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Could not update the command list. The error was reported",
                    ephemeral=True,
                )
        except Exception:
            log.exception("Failed to send CustomCommands list error feedback")


class RawResponseView(discord.ui.View):
    def __init__(
        self,
        cog: CustomCommands,
        *,
        requester_id: int,
        pages: tuple[discord.Embed, ...],
    ):
        super().__init__(timeout=INTERACTIVE_VIEW_TIMEOUT_SECONDS)
        self._cog = cog
        self._requester_id = requester_id
        self._pages = pages
        self._page = 0
        self.message: discord.Message | None = None
        self._previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
        )
        self._next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
        )
        self._previous_button.callback = self._previous
        self._next_button.callback = self._next
        self.add_item(self._previous_button)
        self.add_item(self._next_button)
        self._update_navigation_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this command can use these controls.",
            ephemeral=True,
        )
        return False

    async def _previous(self, interaction: discord.Interaction) -> None:
        self._page -= 1
        self._update_navigation_buttons()
        await interaction.response.edit_message(
            embed=self._pages[self._page],
            view=self,
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        self._page += 1
        self._update_navigation_buttons()
        await interaction.response.edit_message(
            embed=self._pages[self._page],
            view=self,
        )

    def _update_navigation_buttons(self) -> None:
        self._previous_button.disabled = self._page == 0
        self._next_button.disabled = self._page == len(self._pages) - 1

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception as error:
                await self._cog._report_view_timeout_error(
                    self.message,
                    action="expire raw custom command response browser",
                    error=error,
                )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[RawResponseView],
    ) -> None:
        if interaction.guild is not None:
            await self._cog.nhmisc.report_operational_error(
                guild_id=interaction.guild.id,
                source="CustomCommands",
                action="browse raw custom command responses",
                error=error,
                channel_id=getattr(interaction.channel, "id", None),
                message_id=getattr(self.message, "id", None),
            )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Could not change the page. The error was reported",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Could not change the page. The error was reported",
                    ephemeral=True,
                )
        except Exception:
            log.exception("Failed to send CustomCommands pagination error feedback")


class DeleteConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: CustomCommands,
        *,
        command: CustomCommand,
        opener_id: int,
    ):
        super().__init__(timeout=INTERACTIVE_VIEW_TIMEOUT_SECONDS)
        self._cog = cog
        self._command = command
        self._opener_id = opener_id
        self.message: discord.Message | None = None
        confirm = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm.callback = self._confirm
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._opener_id:
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this prompt can use it.",
            ephemeral=True,
        )
        return False

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            await self._cog.catalog.delete(
                guild_id=self._command.guild_id,
                name=self._command.name,
                expected_revision=self._command.revision,
            )
        except StaleRevision:
            await interaction.followup.send(
                "This command changed after the prompt opened. Start again.",
                ephemeral=True,
            )
            return
        await self._cog._log_moderation_action(
            interaction.guild,
            f"{interaction.user} deleted custom command `{self._command.name}`",
        )
        await self._finish("Deleted")

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._finish("Cancelled")

    async def _finish(self, status: str) -> None:
        if self.message is not None:
            embed = discord.Embed(
                title=status,
                description=f"`{self._command.name}`",
            )
            await self.message.edit(embed=embed, view=None)
        self.stop()

    async def on_timeout(self) -> None:
        try:
            await self._finish("Timed out")
        except Exception as error:
            if self.message is not None:
                await self._cog._report_view_timeout_error(
                    self.message,
                    action="expire custom command delete prompt",
                    error=error,
                )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[DeleteConfirmationView],
    ) -> None:
        if interaction.guild is not None:
            await self._cog.nhmisc.report_operational_error(
                guild_id=interaction.guild.id,
                source="CustomCommands",
                action="delete custom command",
                error=error,
                channel_id=getattr(interaction.channel, "id", None),
                message_id=getattr(interaction.message, "id", None),
            )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Delete failed. Check the private error channel.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Delete failed. Check the private error channel.",
                    ephemeral=True,
                )
        except Exception:
            log.exception("Failed to send CustomCommands delete error feedback")


class CustomCommands(commands.Cog):
    """Create and run server-owned text commands."""

    def __init__(
        self,
        bot,
        nhmisc,
        *,
        catalog: CustomCommandCatalog | None = None,
    ):
        super().__init__()
        self.bot = bot
        self.nhmisc = nhmisc
        self._data_root = cog_data_path(raw_name="CustomCommands")
        self.catalog = catalog or CustomCommandCatalog(
            self._data_root / "custom_commands.sqlite"
        )
        self._legacy_config = Config.get_conf(
            None,
            identifier=LEGACY_CONFIG_IDENTIFIER,
            cog_name="CustomCommands",
        )
        self.runtime = CustomCommandRuntime(
            bot,
            self.catalog,
            nhmisc.operational_errors,
            logger=log,
        )
        self.workflows = WorkflowManager(self.catalog, nhmisc, logger=log)

    async def cog_load(self) -> None:
        await self.catalog.initialize()

    async def cog_unload(self) -> None:
        await self.workflows.close_all()

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ) -> None:
        if requester == "discord_deleted_user":
            await redact_custom_command_user_data(
                self.catalog,
                self._legacy_config,
                self._data_root / "migration",
                user_id,
            )

    @commands.Cog.listener()
    async def on_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if getattr(ctx, "cog", None) is not self:
            return
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
        )
        original = getattr(error, "original", error)
        if isinstance(error, expected_types) or isinstance(original, expected_types):
            return
        guild = getattr(ctx, "guild", None)
        if guild is None:
            return
        command = getattr(ctx, "command", None)
        action = getattr(command, "qualified_name", None) or "unknown command"
        await self.nhmisc.report_operational_error(
            guild_id=guild.id,
            source="CustomCommands",
            action=action,
            error=original,
            channel_id=getattr(getattr(ctx, "channel", None), "id", None),
            message_id=getattr(getattr(ctx, "message", None), "id", None),
        )

    async def _log_moderation_action(self, guild, content: str) -> None:
        try:
            await self.nhmisc.send_moderation_log(guild, content)
        except Exception as error:
            await self.nhmisc.report_operational_error(
                guild_id=guild.id,
                source="CustomCommands",
                action="publish custom command moderator log",
                error=error,
            )

    async def _report_view_timeout_error(
        self,
        message: discord.Message,
        *,
        action: str,
        error: Exception,
    ) -> None:
        guild = getattr(message, "guild", None)
        if guild is None:
            log.error(
                "CustomCommands view timeout failed outside a guild",
                exc_info=(type(error), error, error.__traceback__),
            )
            return
        await self.nhmisc.report_operational_error(
            guild_id=guild.id,
            source="CustomCommands",
            action=action,
            error=error,
            channel_id=getattr(getattr(message, "channel", None), "id", None),
            message_id=getattr(message, "id", None),
        )

    @commands.group(name="customcom", aliases=["cc"], invoke_without_command=True)
    @commands.guild_only()
    async def customcom(self, ctx: commands.Context) -> None:
        """Manage and inspect custom commands."""
        embed = discord.Embed(
            title="Custom Commands",
            description="Create weighted text commands and inspect existing commands.",
        )
        lines = []
        for command in sorted(self.customcom.commands, key=lambda item: item.name):
            if command.hidden:
                continue
            usage = f"{ctx.clean_prefix}{command.qualified_name}"
            signature = command.signature.strip()
            if signature:
                usage = f"{usage} {signature}"
            lines.append(f"`{usage}`\n{command.short_doc or 'No description'}")
        embed.add_field(
            name="Commands",
            value="\n".join(lines) or "No commands available",
            inline=False,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _assert_legacy_purge_authority(self) -> None:
        if self.bot.get_cog("CustomCommands") is not self:
            raise commands.UserFeedbackCheckFailure(
                "The managed Custom Commands replacement is not active"
            )
        for name in ("customcom", "cc"):
            command = self.bot.get_command(name)
            if command is None or command.cog is not self:
                raise commands.UserFeedbackCheckFailure(
                    f"The replacement does not own the {name} command"
                )
        packages = await self.bot._config.packages()
        if "customcom" in packages or "customcom" in self.bot.extensions:
            raise commands.UserFeedbackCheckFailure(
                "The official CustomCom package is still active or configured"
            )

    @customcom.command(name="purgelegacy", hidden=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def cc_purgelegacy(
        self,
        ctx: commands.Context,
        confirmation: str | None = None,
    ) -> None:
        """Inspect or permanently remove inactive legacy CustomCom data."""
        await self._assert_legacy_purge_authority()
        database_path = self._data_root / "custom_commands.sqlite"
        if confirmation is None:
            status = await inspect_legacy_data(
                self._legacy_config,
                self._data_root,
                database_path,
            )
            state_label = "present" if status.migration_state_present else "absent"
            if status.is_clean:
                next_step = "No legacy data remains."
            else:
                next_step = (
                    "This permanently deletes only the legacy surfaces above.\n"
                    f"Run `{ctx.clean_prefix}customcom purgelegacy confirm` to continue."
                )
            embed = discord.Embed(
                title="Legacy CustomCom cleanup",
                description=(
                    f"Active SQLite commands: {status.active_command_count}\n"
                    f"Legacy Config commands: {status.legacy_command_count}\n"
                    f"Migration artifact files: {status.artifact_file_count} "
                    f"({status.artifact_bytes} bytes)\n"
                    f"Migration state table: {state_label}\n\n"
                    f"{next_step}"
                ),
            )
            await ctx.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if confirmation != "confirm":
            raise commands.UserFeedbackCheckFailure(
                f"Use `{ctx.clean_prefix}customcom purgelegacy confirm` exactly"
            )
        try:
            await purge_legacy_data(
                self._legacy_config,
                self._data_root,
                database_path,
            )
        except LegacyCleanupPreconditionError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        except LegacyCleanupError:
            raise
        await self._log_moderation_action(
            ctx.guild,
            f"{ctx.author} permanently removed inactive legacy CustomCom data",
        )
        await ctx.send(
            embed=discord.Embed(
                title="Legacy CustomCom data removed",
                description=(
                    "Legacy Config, local migration artifacts, and the migration-state "
                    "table are gone. Run the command again to verify zero remaining targets."
                ),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @customcom.command(name="raw")
    async def cc_raw(self, ctx: commands.Context, command: str) -> None:
        """Show exact stored responses without triggering mentions."""
        stored = await self.catalog.get(ctx.guild.id, command)
        if stored is None:
            await ctx.send(COMMAND_NOT_FOUND_MESSAGE)
            return
        presentations = tuple(
            present_exact_response(response.content) for response in stored.responses
        )
        if any(item.attachment is not None for item in presentations):
            transcript = build_response_transcript(
                tuple(response.content for response in stored.responses)
            )
            await ctx.send(
                file=discord.File(
                    BytesIO(transcript),
                    filename=f"{stored.name}-responses.txt",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        pages = tuple(
            discord.Embed(
                title=f"Response {index}/{len(stored.responses)}",
                description=presentation.description,
            )
            for index, presentation in enumerate(presentations, start=1)
        )
        view = RawResponseView(
            self,
            requester_id=ctx.author.id,
            pages=pages,
        )
        view.message = await ctx.send(
            embed=pages[0],
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @customcom.command(name="search")
    async def cc_search(self, ctx: commands.Context, *, query: str) -> None:
        """Search custom command names with fuzzy matching."""
        stored = await self.catalog.list_commands(ctx.guild.id)
        by_name = {command.name: command for command in stored}
        matches = rapidfuzz.process.extract(
            query,
            tuple(by_name),
            processor=rapidfuzz.utils.default_process,
        )
        accepted = [
            by_name[name]
            for name, score, _index in matches
            if score > FUZZY_MATCH_THRESHOLD
        ]
        if not accepted:
            await ctx.send("No close matches were found.")
            return
        await self._send_command_list(ctx, accepted, title="Search results")

    @customcom.command(name="list")
    async def cc_list(self, ctx: commands.Context) -> None:
        """List all available custom commands."""
        stored = await self.catalog.list_commands(ctx.guild.id)
        if not stored:
            await ctx.send(
                f"There are no custom commands. Use "
                f"`{ctx.clean_prefix}customcom create <name>` to add one."
            )
            return
        await self._send_command_list(ctx, stored, title="Custom Command List")

    async def _send_command_list(
        self,
        ctx: commands.Context,
        stored: list[CustomCommand] | tuple[CustomCommand, ...],
        *,
        title: str,
    ) -> None:
        lines = []
        for command in stored:
            preview = " ".join(command.responses[0].content.split())
            if len(preview) > PREVIEW_LENGTH:
                preview = preview[: PREVIEW_LENGTH - 3] + "..."
            preview = discord.utils.escape_markdown(preview)
            name = discord.utils.escape_markdown(
                f"{ctx.clean_prefix}{command.name}"
            )
            line = f"**{name}**"
            if preview:
                line = f"{line} - {preview}"
            lines.append(line)
        page_lines: list[tuple[str, ...]] = []
        current_page: list[str] = []
        current_length = 0
        for line in lines:
            added_length = len(line) + (1 if current_page else 0)
            if current_page and (
                len(current_page) == COMMANDS_PER_PAGE
                or current_length + added_length > EMBED_PAGE_LENGTH
            ):
                page_lines.append(tuple(current_page))
                current_page = []
                current_length = 0
                added_length = len(line)
            current_page.append(line)
            current_length += added_length
        if current_page:
            page_lines.append(tuple(current_page))
        embeds = []
        for index, page in enumerate(page_lines, start=1):
            embed = discord.Embed(title=title, description="\n".join(page))
            embed.set_footer(text=f"Page {index}/{len(page_lines)}")
            embeds.append(embed)
        pages = tuple(embeds)
        view = CommandListView(
            self,
            requester_id=ctx.author.id,
            pages=pages,
        )
        view.message = await ctx.send(
            embed=pages[0],
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @customcom.command(name="show")
    async def cc_show(self, ctx: commands.Context, command_name: str) -> None:
        """Show responses, weights, cooldowns, and metadata."""
        command = await self.catalog.get(ctx.guild.id, command_name)
        if command is None:
            await ctx.send(COMMAND_NOT_FOUND_MESSAGE)
            return
        member = ctx.guild.get_member(command.author_id)
        author = str(member) if member is not None else command.author_name
        total_weight = sum(response.weight for response in command.responses)
        response_lines = []
        for index, response in enumerate(command.responses, start=1):
            probability = response.weight / total_weight
            response_lines.append(
                f"{index}. weight {response.weight} ({probability:.1%})\n{response.content}"
            )
        header = (
            f"Author: {author}\n"
            f"Created: {discord.utils.format_dt(command.created_at)}\n"
            f"Edited: {self._edited_time_label(command)}\n"
            f"Revision: {command.revision}\n"
            f"Cooldowns: {self._cooldown_label(command)}\n\n"
        )
        pages = tuple(
            pagify(
                header + "\n\n".join(response_lines),
                page_length=EMBED_PAGE_LENGTH,
            )
        )
        embeds = [
            discord.Embed(
                title=f"Custom command: {command.name}",
                description=page,
            )
            for page in pages
        ]
        await menus.menu(ctx, embeds)

    @staticmethod
    def _cooldown_label(command: CustomCommand) -> str:
        return (
            ", ".join(
                f"{scope}: {seconds}s"
                for scope, seconds in sorted(command.cooldowns.items())
            )
            or "none"
        )

    @staticmethod
    def _edited_time_label(command: CustomCommand) -> str:
        if command.edited_at is None:
            return "never"
        return discord.utils.format_dt(command.edited_at)

    @customcom.group(
        name="create",
        aliases=["add"],
        invoke_without_command=True,
    )
    @commands.mod_or_permissions(manage_messages=True)
    async def cc_create(
        self,
        ctx: commands.Context,
        command: str,
        *,
        text: str | None = None,
    ) -> None:
        """Open a thread to create a custom command."""
        await self._open_create_workflow(ctx, command, text)

    @cc_create.command(name="simple")
    @commands.mod_or_permissions(manage_messages=True)
    async def cc_create_simple(
        self,
        ctx: commands.Context,
        command: str,
        *,
        text: str | None = None,
    ) -> None:
        """Open a creation thread, optionally seeded with one response."""
        await self._open_create_workflow(ctx, command, text)

    @cc_create.command(name="random")
    @commands.mod_or_permissions(manage_messages=True)
    async def cc_create_random(
        self,
        ctx: commands.Context,
        command: str,
    ) -> None:
        """Open a creation thread for multiple weighted responses."""
        await self._open_create_workflow(ctx, command, None)

    async def _open_create_workflow(
        self,
        ctx: commands.Context,
        command: str,
        text: str | None,
    ) -> None:
        try:
            normalized = self.catalog.normalize_name(command)
        except CatalogError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        if normalized in (*self.bot.all_commands, *commands.RESERVED_COMMAND_NAMES):
            await ctx.send("A bot command already uses that name")
            return
        if await self.catalog.get(ctx.guild.id, normalized) is not None:
            await ctx.send(
                f"This command already exists. Use "
                f"`{ctx.clean_prefix}customcom edit {normalized}`."
            )
            return
        draft = WorkflowDraft(name=normalized)
        if text is not None:
            draft.responses.append(ResponseDraft(text))
        await self.workflows.open(ctx, draft)

    @customcom.command(name="edit")
    @commands.mod_or_permissions(manage_messages=True)
    async def cc_edit(
        self,
        ctx: commands.Context,
        command: str,
        *,
        text: str | None = None,
    ) -> None:
        """Open a thread to edit an existing custom command."""
        stored = await self.catalog.get(ctx.guild.id, command)
        if stored is None:
            await ctx.send(COMMAND_NOT_FOUND_MESSAGE)
            return
        draft = WorkflowDraft.from_command(stored)
        if text is not None:
            response_id = stored.responses[0].response_id if stored.responses else None
            draft.responses = [ResponseDraft(text, response_id=response_id)]
        await self.workflows.open(ctx, draft)

    @customcom.command(name="cooldown")
    @commands.mod_or_permissions(manage_messages=True)
    async def cc_cooldown(
        self,
        ctx: commands.Context,
        command: str,
        cooldown: int | None = None,
        *,
        per: str = "member",
    ) -> None:
        """Show, set, or remove a member, channel, or guild cooldown."""
        stored = await self.catalog.get(ctx.guild.id, command)
        if stored is None:
            await ctx.send(COMMAND_NOT_FOUND_MESSAGE)
            return
        if cooldown is None:
            await ctx.send(f"Cooldowns: {self._cooldown_label(stored)}")
            return
        normalized_scope = per.casefold()
        scope = {"server": "guild", "user": "member"}.get(
            normalized_scope,
            normalized_scope,
        )
        if scope not in {"member", "channel", "guild"}:
            await ctx.send("Cooldown scope must be member, channel, or guild")
            return
        cooldowns = dict(stored.cooldowns)
        if cooldown <= 0:
            cooldowns.pop(scope, None)
        else:
            cooldowns[scope] = cooldown
        try:
            updated = await self.catalog.edit(
                guild_id=ctx.guild.id,
                name=stored.name,
                expected_revision=stored.revision,
                editor_id=ctx.author.id,
                editor_name=str(ctx.author),
                cooldowns=cooldowns,
            )
        except StaleRevision as error:
            raise commands.UserFeedbackCheckFailure(
                "This command changed while the cooldown was being edited. Try again."
            ) from error
        await self._log_moderation_action(
            ctx.guild,
            f"{ctx.author} updated cooldowns for custom command `{updated.name}`",
        )

    @customcom.command(name="delete", aliases=["del", "remove"])
    @commands.mod_or_permissions(manage_messages=True)
    async def cc_delete(self, ctx: commands.Context, command: str) -> None:
        """Review and delete a custom command."""
        stored = await self.catalog.get(ctx.guild.id, command)
        if stored is None:
            await ctx.send(COMMAND_NOT_FOUND_MESSAGE)
            return
        embed = discord.Embed(
            title="Delete custom command?",
            description=(
                f"Command: `{stored.name}`\nResponses: {len(stored.responses)}\n"
                "This cannot be undone."
            ),
        )
        view = DeleteConfirmationView(
            self,
            command=stored,
            opener_id=ctx.author.id,
        )
        view.message = await ctx.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener()
    async def on_message_without_command(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        try:
            if await self.bot.cog_disabled_in_guild(self, message.guild):
                return
            if await self.workflows.on_message(message):
                return
            await self.runtime.handle_message(message)
        except Exception as error:
            channel = message.channel
            await self.nhmisc.report_operational_error(
                guild_id=message.guild.id,
                source="CustomCommands",
                action="process custom command message",
                error=error,
                channel_id=getattr(channel, "id", None),
                thread_id=(
                    getattr(channel, "id", None)
                    if getattr(channel, "parent", None) is not None
                    else None
                ),
                message_id=message.id,
            )
