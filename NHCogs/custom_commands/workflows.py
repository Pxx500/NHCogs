from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import discord

from .arguments import ArgumentSignatureError, argument_signature
from .catalog import (
    MAX_WEIGHT,
    CatalogError,
    CustomCommand,
    CustomCommandCatalog,
    ResponseDraft,
    StaleRevision,
)

SESSION_TIMEOUT_SECONDS = 30 * 60
MAX_DASHBOARD_RESPONSES = 20
DASHBOARD_PREVIEW_LENGTH = 160
DASHBOARD_FIELD_LENGTH = 1_024
WEIGHT_COMMAND_PARTS = 3
INDEX_COMMAND_PARTS = 2
MOVE_COMMAND_PARTS = 3


class WorkflowInputError(ValueError):
    pass


@dataclass
class WorkflowDraft:
    name: str
    responses: list[ResponseDraft] = field(default_factory=list)
    cooldowns: dict[str, int] = field(default_factory=dict)
    expected_revision: int | None = None
    pending_replacement: int | None = None

    @classmethod
    def from_command(cls, command: CustomCommand) -> WorkflowDraft:
        return cls(
            name=command.name,
            responses=[
                ResponseDraft(
                    content=response.content,
                    weight=response.weight,
                    response_id=response.response_id,
                )
                for response in command.responses
            ],
            cooldowns=dict(command.cooldowns),
            expected_revision=command.revision,
        )

    def process_message(self, content: str) -> str:
        if not content.strip():
            raise WorkflowInputError("Responses cannot be empty")
        if self.pending_replacement is not None:
            index = self.pending_replacement
            current = self.responses[index]
            self.responses[index] = ResponseDraft(
                content=content,
                weight=current.weight,
                response_id=current.response_id,
            )
            self.pending_replacement = None
            return "response replaced"

        parts = content.split()
        command = parts[0].casefold()
        if command == "weight":
            if len(parts) != WEIGHT_COMMAND_PARTS:
                raise WorkflowInputError("Use: weight <response number> <1-1000>")
            index = self._response_index(parts[1])
            try:
                weight = int(parts[2])
            except ValueError as error:
                raise WorkflowInputError("Weight must be a whole number") from error
            if not 1 <= weight <= MAX_WEIGHT:
                raise WorkflowInputError("Weight must be from 1 to 1000")
            current = self.responses[index]
            self.responses[index] = ResponseDraft(
                content=current.content,
                weight=weight,
                response_id=current.response_id,
            )
            return "weight updated"
        if command == "remove":
            if len(parts) != INDEX_COMMAND_PARTS:
                raise WorkflowInputError("Use: remove <response number>")
            self.responses.pop(self._response_index(parts[1]))
            return "response removed"
        if command == "replace":
            if len(parts) != INDEX_COMMAND_PARTS:
                raise WorkflowInputError("Use: replace <response number>")
            self.pending_replacement = self._response_index(parts[1])
            return "replacement requested"
        if command == "move":
            if len(parts) != MOVE_COMMAND_PARTS:
                raise WorkflowInputError("Use: move <response number> <new position>")
            source = self._response_index(parts[1])
            destination = self._response_index(parts[2])
            response = self.responses.pop(source)
            self.responses.insert(destination, response)
            return "response moved"
        self.responses.append(ResponseDraft(content=content))
        return "added"

    def _response_index(self, value: str) -> int:
        try:
            index = int(value) - 1
        except ValueError as error:
            raise WorkflowInputError("Response number must be a whole number") from error
        if not 0 <= index < len(self.responses):
            raise WorkflowInputError("That response does not exist")
        return index


class WorkflowView(discord.ui.View):
    def __init__(self, session: WorkflowSession):
        super().__init__(timeout=None)
        self._session = session
        save = discord.ui.Button(label="Save", style=discord.ButtonStyle.green)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        save.callback = self._save
        cancel.callback = self._cancel
        self.add_item(save)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._session.opener_id:
            self._session.touch()
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this session can use these controls.",
            ephemeral=True,
        )
        return False

    async def _save(self, interaction: discord.Interaction) -> None:
        await self._session.save(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._session.finish("Cancelled")

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[Any],
    ) -> None:
        await self._session.report_interaction_error(interaction, error)


class WorkflowSession:
    def __init__(
        self,
        manager: WorkflowManager,
        *,
        thread: discord.Thread,
        opener: discord.Member,
        draft: WorkflowDraft,
    ):
        self._manager = manager
        self.thread = thread
        self.opener_id = opener.id
        self.opener_name = str(opener)
        self.draft = draft
        self.dashboard: discord.Message | None = None
        self.view = WorkflowView(self)
        self.status = "Editing"
        self.validation_error: str | None = None
        self.finished = False
        self._timeout_task: asyncio.Task[None] | None = None

    def touch(self) -> None:
        if self.finished:
            return
        if self._timeout_task is not None:
            self._timeout_task.cancel()
        self._timeout_task = asyncio.create_task(self._expire_after_inactivity())

    async def _expire_after_inactivity(self) -> None:
        try:
            await asyncio.sleep(self._manager.session_timeout_seconds)
        except asyncio.CancelledError:
            return
        try:
            await self.finish("Timed out")
        except Exception as error:
            await self._manager._report_failure(
                guild_id=self.thread.guild.id,
                action="expire inactive custom command workflow",
                error=error,
                thread_id=self.thread.id,
            )

    def render_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Custom command: {self.draft.name}",
            description=f"Status: {self.status}",
        )
        total_weight = sum(response.weight for response in self.draft.responses)
        lines: list[str] = []
        displayed = 0
        for index, response in enumerate(
            self.draft.responses[:MAX_DASHBOARD_RESPONSES],
            start=1,
        ):
            preview = response.content.replace("\n", " ")
            if len(preview) > DASHBOARD_PREVIEW_LENGTH:
                preview = preview[: DASHBOARD_PREVIEW_LENGTH - 3] + "..."
            probability = (
                f"{response.weight / total_weight:.1%}" if total_weight else "0%"
            )
            line = f"{index}. weight {response.weight} ({probability})\n{preview}"
            candidate = "\n\n".join((*lines, line))
            if len(candidate) > DASHBOARD_FIELD_LENGTH - 40:
                break
            lines.append(line)
            displayed += 1
        hidden = len(self.draft.responses) - displayed
        if hidden > 0:
            lines.append(f"+{hidden} more responses")
        embed.add_field(
            name="Responses",
            value="\n\n".join(lines) or "No responses yet",
            inline=False,
        )
        embed.add_field(
            name="Cooldowns",
            value=(
                ", ".join(
                    f"{scope}: {seconds}s"
                    for scope, seconds in sorted(self.draft.cooldowns.items())
                )
                or "None"
            ),
            inline=False,
        )
        signature = self._signature_label()
        embed.add_field(name="Arguments", value=signature, inline=False)
        controls = (
            "Send a message to add a response.\n"
            "`weight <number> <1-1000>`\n"
            "`remove <number>`\n"
            "`replace <number>`, then send the replacement\n"
            "`move <number> <new position>`"
        )
        embed.add_field(name="Controls", value=controls, inline=False)
        if self.validation_error is not None:
            embed.add_field(
                name="Error",
                value=self.validation_error[:1_024],
                inline=False,
            )
        return embed

    def _signature_label(self) -> str:
        if not self.draft.responses:
            return "None"
        try:
            signatures = tuple(
                argument_signature(response.content)
                for response in self.draft.responses
            )
        except ArgumentSignatureError as error:
            return str(error)
        if any(signature != signatures[0] for signature in signatures[1:]):
            return "Responses use different argument signatures"
        if not signatures[0]:
            return "None"
        return ", ".join(
            f"argument {index + 1}: {converter or 'text'}"
            for index, converter in enumerate(signatures[0])
        )

    async def handle_message(self, message: discord.Message) -> None:
        if self.finished or message.author.id != self.opener_id:
            return
        self.touch()
        try:
            outcome = self.draft.process_message(message.content)
        except WorkflowInputError as error:
            self.validation_error = str(error)
            await self.thread.send(
                str(error),
                allowed_mentions=discord.AllowedMentions.none(),
                delete_after=15,
            )
        else:
            self.validation_error = (
                "Send the replacement response now."
                if outcome == "replacement requested"
                else None
            )
        await self.update_dashboard()

    async def update_dashboard(self) -> None:
        if self.dashboard is not None:
            await self.dashboard.edit(embed=self.render_embed(), view=self.view)

    async def save(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            if self.draft.expected_revision is None:
                await self._manager.catalog.create(
                    guild_id=self.thread.guild.id,
                    name=self.draft.name,
                    author_id=self.opener_id,
                    author_name=self.opener_name,
                    responses=tuple(self.draft.responses),
                    cooldowns=self.draft.cooldowns,
                )
            else:
                await self._manager.catalog.edit(
                    guild_id=self.thread.guild.id,
                    name=self.draft.name,
                    expected_revision=self.draft.expected_revision,
                    editor_id=self.opener_id,
                    editor_name=self.opener_name,
                    responses=tuple(self.draft.responses),
                    cooldowns=self.draft.cooldowns,
                )
        except StaleRevision:
            self.validation_error = (
                "This command changed after the session opened. Start a new edit session."
            )
            await interaction.followup.send(self.validation_error, ephemeral=True)
            await self.update_dashboard()
            return
        except CatalogError as error:
            self.validation_error = str(error)
            await interaction.followup.send(self.validation_error, ephemeral=True)
            await self.update_dashboard()
            return
        await self._manager.log_moderation_action(
            self.thread.guild,
            (
                f"{self.opener_name} saved custom command `{self.draft.name}` "
                f"in {self.thread.mention}"
            ),
        )
        await self.finish("Saved")

    async def finish(self, status: str) -> None:
        if self.finished:
            return
        self.finished = True
        current_task = asyncio.current_task()
        if self._timeout_task is not None and self._timeout_task is not current_task:
            self._timeout_task.cancel()
        self._timeout_task = None
        self.status = status
        for item in self.view.children:
            item.disabled = True
        if self.dashboard is not None:
            try:
                await self.dashboard.edit(embed=self.render_embed(), view=self.view)
            except Exception as error:
                await self._manager._report_failure(
                    guild_id=self.thread.guild.id,
                    action="update completed custom command workflow",
                    error=error,
                    thread_id=self.thread.id,
                    message_id=self.dashboard.id,
                )
        self.view.stop()
        self._manager.remove(self.thread.id)
        try:
            await self.thread.edit(archived=True, locked=True)
        except discord.HTTPException as error:
            await self._manager._report_failure(
                guild_id=self.thread.guild.id,
                action="archive completed custom command workflow",
                error=error,
                thread_id=self.thread.id,
            )

    async def report_interaction_error(
        self,
        interaction: discord.Interaction,
        error: BaseException,
    ) -> None:
        await self._manager.report_interaction_error(
            interaction,
            "process workflow control",
            error,
        )


class WorkflowManager:
    def __init__(
        self,
        catalog: CustomCommandCatalog,
        nhmisc: Any,
        *,
        logger: logging.Logger,
        session_timeout_seconds: float = SESSION_TIMEOUT_SECONDS,
    ):
        self.catalog = catalog
        self._nhmisc = nhmisc
        self._operational_errors = nhmisc.operational_errors
        self.logger = logger
        self.session_timeout_seconds = session_timeout_seconds
        self._sessions: dict[int, WorkflowSession] = {}

    async def open(
        self,
        ctx: Any,
        draft: WorkflowDraft,
    ) -> WorkflowSession:
        thread = await ctx.message.create_thread(
            name=f"customcom-{draft.name}"[:100],
            auto_archive_duration=60,
        )
        session = WorkflowSession(
            self,
            thread=thread,
            opener=ctx.author,
            draft=draft,
        )
        try:
            session.dashboard = await thread.send(
                f"Editing started by {ctx.author}",
                embed=session.render_embed(),
                view=session.view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            try:
                await thread.edit(archived=True, locked=True)
            except Exception as cleanup_error:
                await self._report_failure(
                    guild_id=thread.guild.id,
                    action="clean up failed custom command workflow",
                    error=cleanup_error,
                    thread_id=thread.id,
                )
            raise
        self._sessions[thread.id] = session
        session.touch()
        return session

    async def on_message(self, message: discord.Message) -> bool:
        session = self._sessions.get(message.channel.id)
        if session is None or message.author.bot:
            return False
        try:
            await session.handle_message(message)
        except Exception as error:
            await self._report(message, "process workflow message", error)
        return True

    def remove(self, thread_id: int) -> None:
        self._sessions.pop(thread_id, None)

    async def close_all(self) -> None:
        for session in tuple(self._sessions.values()):
            try:
                await session.finish("Reloaded")
            except Exception as error:
                await self._report_failure(
                    guild_id=session.thread.guild.id,
                    action="close workflow during reload",
                    error=error,
                    thread_id=session.thread.id,
                )

    async def log_moderation_action(self, guild: Any, content: str) -> None:
        try:
            await self._nhmisc.send_moderation_log(guild, content)
        except Exception as error:
            await self._report_failure(
                guild_id=guild.id,
                action="publish custom command moderator log",
                error=error,
            )

    async def _report(
        self,
        message: discord.Message,
        action: str,
        error: BaseException,
    ) -> None:
        await self._report_failure(
            guild_id=message.guild.id,
            action=action,
            error=error,
            thread_id=message.channel.id,
            message_id=message.id,
        )

    async def report_interaction_error(
        self,
        interaction: discord.Interaction,
        action: str,
        error: BaseException,
    ) -> None:
        await self._report_failure(
            guild_id=interaction.guild_id,
            action=action,
            error=error,
            thread_id=getattr(interaction.channel, "id", None),
            message_id=getattr(interaction.message, "id", None),
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "The workflow action failed. Check the private error channel.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "The workflow action failed. Check the private error channel.",
                    ephemeral=True,
                )
        except Exception:
            self.logger.exception("Failed to send CustomCommands workflow error feedback")

    async def _report_failure(
        self,
        *,
        guild_id: int,
        action: str,
        error: BaseException,
        thread_id: int | None = None,
        message_id: int | None = None,
    ) -> None:
        try:
            await self._operational_errors.report(
                guild_id=guild_id,
                source="CustomCommands",
                action=action,
                error=error,
                thread_id=thread_id,
                message_id=message_id,
            )
        except Exception:
            self.logger.exception("Failed to report CustomCommands workflow error")
