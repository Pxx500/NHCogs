from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import discord

from .arguments import ArgumentSignatureError, argument_signature
from .catalog import (
    MAX_RESPONSE_LENGTH,
    MAX_WEIGHT,
    CatalogError,
    CustomCommand,
    CustomCommandCatalog,
    ResponseDraft,
    StaleRevision,
)
from .presentation import present_exact_response

SESSION_TIMEOUT_SECONDS = 30 * 60
RESPONSES_PER_PAGE = 5
DASHBOARD_PREVIEW_LENGTH = 160
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

    def add_response(self, content: str) -> int:
        self._validate_content(content)
        self.responses.append(ResponseDraft(content=content))
        return len(self.responses) - 1

    def replace_response(self, index: int, content: str) -> None:
        self._validate_content(content)
        index = self._existing_index(index)
        current = self.responses[index]
        self.responses[index] = ResponseDraft(
            content=content,
            weight=current.weight,
            response_id=current.response_id,
        )

    def remove_response(self, index: int) -> ResponseDraft:
        return self.responses.pop(self._existing_index(index))

    def set_weight(self, index: int, weight: int) -> None:
        index = self._existing_index(index)
        if type(weight) is not int or not 1 <= weight <= MAX_WEIGHT:
            raise WorkflowInputError("Weight must be from 1 to 1000")
        current = self.responses[index]
        self.responses[index] = ResponseDraft(
            content=current.content,
            weight=weight,
            response_id=current.response_id,
        )

    def move_response(self, source: int, destination: int) -> int:
        source = self._existing_index(source)
        destination = self._existing_index(destination)
        response = self.responses.pop(source)
        self.responses.insert(destination, response)
        return destination

    def process_message(self, content: str) -> str:
        self._validate_content(content)
        if self.pending_replacement is not None:
            index = self.pending_replacement
            self.replace_response(index, content)
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
            self.set_weight(index, weight)
            return "weight updated"
        if command == "remove":
            if len(parts) != INDEX_COMMAND_PARTS:
                raise WorkflowInputError("Use: remove <response number>")
            self.remove_response(self._response_index(parts[1]))
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
            self.move_response(source, destination)
            return "response moved"
        self.add_response(content)
        return "added"

    @staticmethod
    def _validate_content(content: str) -> None:
        if not content.strip():
            raise WorkflowInputError("Responses cannot be empty")

    def _existing_index(self, index: int) -> int:
        if type(index) is not int or not 0 <= index < len(self.responses):
            raise WorkflowInputError("That response does not exist")
        return index

    def _response_index(self, value: str) -> int:
        try:
            index = int(value) - 1
        except ValueError as error:
            raise WorkflowInputError("Response number must be a whole number") from error
        if not 0 <= index < len(self.responses):
            raise WorkflowInputError("That response does not exist")
        return index


class ResponseModal(discord.ui.Modal):
    def __init__(self, session: WorkflowSession, *, editing: bool):
        super().__init__(title="Edit response" if editing else "Add response")
        self._session = session
        self._editing = editing
        default = None
        if editing:
            default = session.draft.responses[session._require_selection()].content
        self.content = discord.ui.TextInput(
            label="Response",
            style=discord.TextStyle.paragraph,
            default=default,
            required=True,
            max_length=MAX_RESPONSE_LENGTH,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            if self._editing:
                self._session.replace_selected(self.content.value)
            else:
                self._session.add_response(self.content.value)
        except WorkflowInputError as error:
            self._session.validation_error = str(error)
            await interaction.response.send_message(str(error), ephemeral=True)
            await self._session.update_dashboard()
            return
        self._session.validation_error = None
        self._session.delete_confirmation_index = None
        await interaction.response.defer()
        await self._session.update_dashboard()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await self._session.report_interaction_error(interaction, error)


class NumberModal(discord.ui.Modal):
    def __init__(self, session: WorkflowSession, *, action: str):
        if action not in {"weight", "move"}:
            raise ValueError("Unknown workflow number action")
        super().__init__(title="Set response weight" if action == "weight" else "Move response")
        self._session = session
        self._action = action
        selected = session._require_selection()
        current = session.draft.responses[selected]
        self.value = discord.ui.TextInput(
            label="Weight" if action == "weight" else "New position",
            style=discord.TextStyle.short,
            default=str(current.weight if action == "weight" else selected + 1),
            required=True,
            max_length=4,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            number = int(self.value.value)
        except ValueError:
            message = (
                "Weight must be a whole number"
                if self._action == "weight"
                else "Response number must be a whole number"
            )
            await interaction.response.send_message(message, ephemeral=True)
            return
        try:
            if self._action == "weight":
                self._session.set_selected_weight(number)
            else:
                self._session.move_selected(number - 1)
        except WorkflowInputError as error:
            self._session.validation_error = str(error)
            await interaction.response.send_message(str(error), ephemeral=True)
            await self._session.update_dashboard()
            return
        self._session.validation_error = None
        self._session.delete_confirmation_index = None
        await interaction.response.defer()
        await self._session.update_dashboard()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await self._session.report_interaction_error(interaction, error)


class WorkflowView(discord.ui.View):
    def __init__(self, session: WorkflowSession):
        super().__init__(timeout=None)
        self._session = session
        self.refresh()

    def refresh(self) -> None:
        self.clear_items()
        visible = self._session.visible_response_indices
        if visible:
            select = discord.ui.Select(
                placeholder="Select a response",
                options=[
                    discord.SelectOption(
                        label=f"#{index + 1}",
                        value=str(index),
                        default=index == self._session.selected_index,
                    )
                    for index in visible
                ],
                row=0,
            )
            select.callback = self._select_response
            self.add_item(select)

        has_selection = self._session.selected_index is not None
        controls = (
            (
                "Previous",
                discord.ButtonStyle.secondary,
                1,
                self._previous,
                self._session.page == 0,
            ),
            (
                "Next",
                discord.ButtonStyle.secondary,
                1,
                self._next,
                self._session.page == self._session.page_count - 1,
            ),
            ("Add", discord.ButtonStyle.primary, 1, self._add, False),
            ("Edit", discord.ButtonStyle.secondary, 1, self._edit, not has_selection),
            (
                (
                    "Confirm delete"
                    if self._session.delete_confirmation_index
                    == self._session.selected_index
                    and has_selection
                    else "Delete"
                ),
                discord.ButtonStyle.danger,
                1,
                self._delete,
                not has_selection,
            ),
            ("Weight", discord.ButtonStyle.secondary, 2, self._weight, not has_selection),
            ("Move", discord.ButtonStyle.secondary, 2, self._move, not has_selection),
            (
                "View exact",
                discord.ButtonStyle.secondary,
                2,
                self._view_exact,
                not has_selection,
            ),
            (
                "Save",
                discord.ButtonStyle.green,
                2,
                self._save,
                not self._session.draft.responses,
            ),
            ("Cancel", discord.ButtonStyle.secondary, 2, self._cancel, False),
        )
        for label, style, row, callback, disabled in controls:
            button = discord.ui.Button(label=label, style=style, row=row)
            button.callback = callback
            button.disabled = disabled
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._session.opener_id:
            self._session.touch()
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this session can use these controls.",
            ephemeral=True,
        )
        return False

    async def _select_response(self, interaction: discord.Interaction) -> None:
        select = next(
            item for item in self.children if isinstance(item, discord.ui.Select)
        )
        self._session.select_response(int(select.values[0]))
        self._session.delete_confirmation_index = None
        self._session.validation_error = None
        await self._edit_dashboard(interaction)

    async def _previous(self, interaction: discord.Interaction) -> None:
        self._session.set_page(self._session.page - 1)
        self._session.delete_confirmation_index = None
        self._session.validation_error = None
        await self._edit_dashboard(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self._session.set_page(self._session.page + 1)
        self._session.delete_confirmation_index = None
        self._session.validation_error = None
        await self._edit_dashboard(interaction)

    async def _add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ResponseModal(self._session, editing=False))

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ResponseModal(self._session, editing=True))

    async def _delete(self, interaction: discord.Interaction) -> None:
        selected = self._session._require_selection()
        if self._session.delete_confirmation_index != selected:
            self._session.delete_confirmation_index = selected
            self._session.validation_error = "Click Confirm delete to remove this response."
        else:
            self._session.remove_selected()
            self._session.delete_confirmation_index = None
            self._session.validation_error = None
        await self._edit_dashboard(interaction)

    async def _weight(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NumberModal(self._session, action="weight"))

    async def _move(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NumberModal(self._session, action="move"))

    async def _view_exact(self, interaction: discord.Interaction) -> None:
        await self._session.send_exact_response(interaction)

    async def _edit_dashboard(self, interaction: discord.Interaction) -> None:
        self.refresh()
        await interaction.response.edit_message(
            embed=self._session.render_embed(),
            view=self,
        )

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
        self.status = "Editing"
        self.validation_error: str | None = None
        self.finished = False
        self.page = 0
        self.selected_index: int | None = None
        self.delete_confirmation_index: int | None = None
        self._timeout_task: asyncio.Task[None] | None = None
        self.view = WorkflowView(self)

    @property
    def page_count(self) -> int:
        return max(
            1,
            (len(self.draft.responses) + RESPONSES_PER_PAGE - 1)
            // RESPONSES_PER_PAGE,
        )

    @property
    def visible_response_indices(self) -> tuple[int, ...]:
        start = self.page * RESPONSES_PER_PAGE
        stop = min(start + RESPONSES_PER_PAGE, len(self.draft.responses))
        return tuple(range(start, stop))

    def set_page(self, page: int) -> None:
        self.page = max(0, min(page, self.page_count - 1))
        if self.selected_index not in self.visible_response_indices:
            self.selected_index = None

    def select_response(self, index: int) -> None:
        if index not in self.visible_response_indices:
            raise WorkflowInputError("That response is not on this page")
        self.selected_index = index

    def add_response(self, content: str) -> int:
        index = self.draft.add_response(content)
        self._show_response(index)
        return index

    def replace_selected(self, content: str) -> None:
        self.draft.replace_response(self._require_selection(), content)

    def remove_selected(self) -> ResponseDraft:
        index = self._require_selection()
        removed = self.draft.remove_response(index)
        if not self.draft.responses:
            self.page = 0
            self.selected_index = None
        else:
            self._show_response(min(index, len(self.draft.responses) - 1))
        return removed

    def set_selected_weight(self, weight: int) -> None:
        self.draft.set_weight(self._require_selection(), weight)

    def move_selected(self, destination: int) -> int:
        moved = self.draft.move_response(self._require_selection(), destination)
        self._show_response(moved)
        return moved

    def _require_selection(self) -> int:
        if self.selected_index is None:
            raise WorkflowInputError("Select a response first")
        return self.selected_index

    def _show_response(self, index: int) -> None:
        self.page = index // RESPONSES_PER_PAGE
        self.selected_index = index

    async def send_exact_response(self, interaction: discord.Interaction) -> None:
        index = self._require_selection()
        content = self.draft.responses[index].content
        presentation = present_exact_response(content)
        if presentation.description is not None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title=f"Response {index + 1}",
                    description=presentation.description,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            file=discord.File(
                BytesIO(presentation.attachment or b""),
                filename=f"{self.draft.name}-response-{index + 1}.txt",
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
        response_count = len(self.draft.responses)
        total_weight = sum(response.weight for response in self.draft.responses)
        embed = discord.Embed(
            title=f"Custom command: {self.draft.name}",
            description=(
                f"{self.status} · {response_count} responses · "
                f"total weight {total_weight}"
            ),
        )
        lines: list[str] = []
        visible = self.visible_response_indices
        for index in visible:
            response = self.draft.responses[index]
            preview = response.content.replace("\n", " ↵ ")
            if len(preview) > DASHBOARD_PREVIEW_LENGTH:
                preview = preview[: DASHBOARD_PREVIEW_LENGTH - 3] + "..."
            probability = (
                f"{response.weight / total_weight:.1%}" if total_weight else "0%"
            )
            lines.append(
                f"#{index + 1} · weight {response.weight} · {probability}\n{preview}"
            )
        if visible:
            response_field_name = (
                f"Responses {visible[0] + 1}-{visible[-1] + 1} of {response_count}"
            )
        else:
            response_field_name = "Responses"
        embed.add_field(
            name=response_field_name,
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
            inline=True,
        )
        signature = self._signature_label()
        embed.add_field(name="Arguments", value=signature, inline=True)
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
        pending_replacement = self.draft.pending_replacement
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
            if outcome == "added":
                self._show_response(len(self.draft.responses) - 1)
            elif outcome == "replacement requested":
                self._show_response(self.draft.pending_replacement or 0)
            elif outcome == "response replaced" and pending_replacement is not None:
                self._show_response(pending_replacement)
            elif outcome == "response removed":
                self.set_page(self.page)
        await self.update_dashboard()

    async def update_dashboard(self) -> None:
        if self.dashboard is not None:
            self.view.refresh()
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
