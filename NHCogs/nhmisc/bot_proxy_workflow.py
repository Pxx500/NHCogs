from __future__ import annotations

import asyncio
import copy
from enum import Enum
from typing import TYPE_CHECKING, Any

import discord

from .bot_proxy import (
    MAX_PROXY_CONTENT_LENGTH,
    ActiveSession,
    BotProxyDraft,
    BotProxySession,
    IdentityType,
    ProxyDestination,
    ProxyIdentity,
    SessionStatus,
)
from .bot_proxy_store import CharacterPreset

if TYPE_CHECKING:
    from .bot_proxy_manager import BotProxyWorkflowManager

SESSION_TIMEOUT_SECONDS = 10 * 60
PRESETS_PER_PAGE = 20
CONTENT_PREVIEW_LENGTH = 300
BOT_PROXY_WORKFLOW_BUTTONS = (
    "Set destination: choose a channel, channel ID, or message link for a reply\n"
    "Set content: enter the message text\n"
    "Identity: use the bot or choose a character. Characters cannot reply\n"
    "Persistent Messaging: keep destination and identity after sending\n"
    "Send Confirmation: require preview and confirmation before publishing\n"
    "Send or Send now: publish the current draft\n"
    "Cancel: close the session"
)


def bot_proxy_guide_embed() -> discord.Embed:
    return discord.Embed(
        title="Bot Proxy guide",
        description=BOT_PROXY_WORKFLOW_BUTTONS,
        color=discord.Color.blue(),
    )


class InputMode(str, Enum):
    DESTINATION = "destination"
    CONTENT = "content"
    AVATAR = "avatar"


class WorkflowInputError(ValueError):
    pass


async def _interaction_is_authorized(
    owner_id: int,
    interaction: discord.Interaction,
    *,
    control: str,
) -> bool:
    if interaction.user.id != owner_id:
        await interaction.response.send_message(
            f"Only the moderator who opened this {control} can use it",
            ephemeral=True,
        )
        return False
    permissions = getattr(interaction, "permissions", None)
    if permissions is None or not permissions.manage_messages:
        await interaction.response.send_message(
            "You need Manage Messages permission",
            ephemeral=True,
        )
        return False
    return True


def _no_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions.none()


def _moderator_mention(member: discord.Member) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        users=[member],
        roles=False,
        replied_user=False,
    )


def _get_channel_or_thread(guild: discord.Guild, channel_id: int) -> Any:
    resolver = getattr(guild, "get_channel_or_thread", guild.get_channel)
    return resolver(channel_id)


class CharacterModal(discord.ui.Modal):
    def __init__(
        self,
        session: BotProxyWorkflowSession,
        *,
        preset: CharacterPreset | None = None,
        save_preset: bool = False,
    ) -> None:
        title = "Save character" if save_preset else "One-time character"
        super().__init__(title=title)
        self.session = session
        self.preset = preset
        self.save_preset = save_preset
        self.preset_name = discord.ui.TextInput(
            label="Preset name",
            required=save_preset,
            max_length=80,
            default=preset.preset_name if preset is not None else None,
        )
        self.display_name = discord.ui.TextInput(
            label="Public name",
            required=True,
            max_length=80,
            default=preset.display_name if preset is not None else None,
        )
        self.avatar_url = discord.ui.TextInput(
            label="Avatar HTTPS URL (optional)",
            required=False,
            max_length=2000,
        )
        if save_preset:
            self.add_item(self.preset_name)
        self.add_item(self.display_name)
        self.add_item(self.avatar_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _interaction_is_authorized(
            self.session.opener_id,
            interaction,
            control="session",
        ):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            self.session.ensure_character_allowed()
            avatar = await self.session.manager.load_avatar_url(
                str(self.avatar_url.value).strip()
            )
            if self.save_preset:
                preset_name = str(self.preset_name.value)
                if self.preset is None:
                    preset = await self.session.manager.store.create_character(
                        guild_id=self.session.guild.id,
                        preset_name=preset_name,
                        display_name=str(self.display_name.value),
                        avatar_bytes=avatar.data if avatar is not None else None,
                        avatar_media_type=(
                            avatar.media_type if avatar is not None else None
                        ),
                        moderator_id=interaction.user.id,
                    )
                else:
                    preset = await self.session.manager.store.update_character(
                        guild_id=self.session.guild.id,
                        preset_name=self.preset.preset_name,
                        expected_revision=self.preset.revision,
                        new_preset_name=preset_name,
                        display_name=str(self.display_name.value),
                        avatar_bytes=(
                            avatar.data
                            if avatar is not None
                            else self.preset.avatar_bytes
                        ),
                        avatar_media_type=(
                            avatar.media_type
                            if avatar is not None
                            else self.preset.avatar_media_type
                        ),
                        moderator_id=interaction.user.id,
                    )
                self.session.set_character(preset)
                await self.session.manager.log_preset_change(
                    self.session.guild,
                    interaction.user,
                    "updated" if self.preset is not None else "created",
                    preset,
                )
            else:
                self.session.set_identity(ProxyIdentity(
                    IdentityType.CHARACTER,
                    display_name=str(self.display_name.value).strip(),
                    avatar_bytes=avatar.data if avatar is not None else None,
                    avatar_media_type=avatar.media_type if avatar is not None else None,
                    avatar_sha256=avatar.sha256 if avatar is not None else None,
                ))
            await self.session.refresh()
            await interaction.edit_original_response(content="Character selected")
        except Exception as error:  # noqa: BLE001
            await self.session.manager.handle_expected_or_reported_error(
                interaction,
                error,
                action="configure Bot Proxy character",
                session=self.session,
            )


class IdentityPickerView(discord.ui.View):
    def __init__(
        self,
        session: BotProxyWorkflowSession,
        presets: tuple[CharacterPreset, ...],
        *,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=30)
        self.session = session
        self.presets = presets
        self.page = page
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _interaction_is_authorized(
            self.session.opener_id,
            interaction,
            control="session",
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await self.session.manager.handle_expected_or_reported_error(
            interaction,
            error,
            action=f"use Bot Proxy identity control {type(item).__name__}",
            session=self.session,
        )

    def _build(self) -> None:
        start = self.page * PRESETS_PER_PAGE
        page_presets = self.presets[start : start + PRESETS_PER_PAGE]
        options = [
            discord.SelectOption(label="Bot", value="bot"),
            discord.SelectOption(label="One-time character", value="one-time"),
            *(
                discord.SelectOption(
                    label=preset.preset_name,
                    description=preset.display_name,
                    value=f"preset:{preset.preset_name}",
                )
                for preset in page_presets
            ),
        ]
        select = discord.ui.Select(
            placeholder="Choose identity",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )
        select.callback = self._select
        self.add_item(select)
        create = discord.ui.Button(
            label="Save new character",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        create.callback = self._create
        self.add_item(create)
        if self.session.draft.identity.kind is IdentityType.CHARACTER:
            upload = discord.ui.Button(
                label="Upload avatar",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            upload.callback = self._upload
            self.add_item(upload)
        if self.session.draft.identity.preset_name is not None:
            edit = discord.ui.Button(
                label="Edit preset",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            edit.callback = self._edit
            self.add_item(edit)
            delete = discord.ui.Button(
                label="Delete preset",
                style=discord.ButtonStyle.danger,
                row=1,
            )
            delete.callback = self._delete
            self.add_item(delete)
        previous = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=self.page == 0,
        )
        previous.callback = self._previous
        self.add_item(previous)
        has_next = start + PRESETS_PER_PAGE < len(self.presets)
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=not has_next,
        )
        next_button.callback = self._next
        self.add_item(next_button)

    async def _select(self, interaction: discord.Interaction) -> None:
        value = interaction.data["values"][0]
        if value == "bot":
            self.session.draft.identity = ProxyIdentity(IdentityType.BOT)
            await self.session.refresh()
            await interaction.response.edit_message(content="Bot selected", view=None)
            return
        if value == "one-time":
            await interaction.response.send_modal(CharacterModal(self.session))
            return
        preset_name = value.removeprefix("preset:")
        preset = next(item for item in self.presets if item.preset_name == preset_name)
        self.session.set_character(preset)
        await self.session.refresh()
        await interaction.response.edit_message(content="Character selected", view=None)

    async def _create(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            CharacterModal(self.session, save_preset=True)
        )

    async def _upload(self, interaction: discord.Interaction) -> None:
        await self.session.begin_input(InputMode.AVATAR, interaction)
        await interaction.response.edit_message(
            content="Send one image attachment in the Bot Proxy thread",
            view=None,
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        preset_name = self.session.draft.identity.preset_name
        if preset_name is None:
            await interaction.response.edit_message(
                content="Select a saved character preset first",
                view=None,
            )
            return
        preset = await self.session.manager.store.get_character(
            self.session.guild.id,
            preset_name,
        )
        if preset is None:
            await interaction.response.edit_message(
                content="This character preset no longer exists",
                view=None,
            )
            return
        await interaction.response.send_modal(
            CharacterModal(self.session, preset=preset, save_preset=True)
        )

    async def _delete(self, interaction: discord.Interaction) -> None:
        preset_name = self.session.draft.identity.preset_name
        if preset_name is None:
            await interaction.response.edit_message(
                content="Select a saved character preset first",
                view=None,
            )
            return
        preset = await self.session.manager.store.get_character(
            self.session.guild.id,
            preset_name,
        )
        if preset is None:
            await interaction.response.edit_message(
                content="This character preset no longer exists",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=f"Delete character preset {preset.preset_name}?",
            view=ConfirmDeleteCharacterView(self.session, preset),
        )

    async def _previous(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=IdentityPickerView(
                self.session,
                self.presets,
                page=max(0, self.page - 1),
            )
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=IdentityPickerView(self.session, self.presets, page=self.page + 1)
        )


class ConfirmDeleteCharacterView(discord.ui.View):
    def __init__(
        self,
        session: BotProxyWorkflowSession,
        preset: CharacterPreset,
    ) -> None:
        super().__init__(timeout=30)
        self.session = session
        self.preset = preset
        confirm = discord.ui.Button(
            label="Delete",
            style=discord.ButtonStyle.danger,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)
        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _interaction_is_authorized(
            self.session.opener_id,
            interaction,
            control="session",
        )

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        deleted = await self.session.manager.store.delete_character(
            guild_id=self.session.guild.id,
            preset_name=self.preset.preset_name,
            expected_revision=self.preset.revision,
        )
        self.session.draft.identity = ProxyIdentity(IdentityType.BOT)
        await self.session.refresh()
        await self.session.manager.log_preset_change(
            self.session.guild,
            interaction.user,
            "deleted",
            deleted,
        )
        await interaction.edit_original_response(
            content="Character preset deleted",
            view=None,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled", view=None)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await self.session.manager.handle_expected_or_reported_error(
            interaction,
            error,
            action=f"delete Bot Proxy character {type(item).__name__}",
            session=self.session,
        )


class DashboardView(discord.ui.View):
    def __init__(self, session: BotProxyWorkflowSession) -> None:
        super().__init__(timeout=None)
        self.session = session
        busy = getattr(session, "_publishing", False)
        self._add_button("Set destination", self._destination, row=0, disabled=busy)
        self._add_button("Set content", self._content, row=0, disabled=busy)
        self._add_button("Identity", self._identity, row=0, disabled=busy)
        self._add_button("Help", self._help, row=0, disabled=busy)
        standalone_destination = (
            session.draft.destination is not None
            and session.draft.destination.message_id is None
        )
        self._add_button(
            "Persistent Messaging: "
            + ("On" if getattr(session, "persistent_messaging", False) else "Off"),
            self._persistent,
            row=1,
            disabled=busy or not standalone_destination,
        )
        self._add_button(
            "Send Confirmation: "
            + ("On" if getattr(session, "send_confirmation", True) else "Off"),
            self._confirmation,
            row=1,
            disabled=busy,
        )
        self._add_button(
            "Send" if getattr(session, "send_confirmation", True) else "Send now",
            self._send,
            row=2,
            primary=True,
            disabled=busy or bool(session.draft.validation_errors()),
        )
        self._add_button("Cancel", self._cancel, row=2, danger=True, disabled=busy)

    def _add_button(
        self,
        label: str,
        callback: Any,
        *,
        row: int,
        primary: bool = False,
        danger: bool = False,
        disabled: bool = False,
    ) -> None:
        style = discord.ButtonStyle.secondary
        if primary:
            style = discord.ButtonStyle.primary
        if danger:
            style = discord.ButtonStyle.danger
        button = discord.ui.Button(
            label=label,
            style=style,
            row=row,
            disabled=disabled,
        )
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = await _interaction_is_authorized(
            self.session.opener_id,
            interaction,
            control="session",
        )
        if allowed and (
            self.session._publishing or self.session._publish_lock.locked()
        ):
            await self.session.manager.private_feedback(
                interaction,
                "Bot Proxy is already sending this message",
            )
            return False
        if allowed:
            self.session.touch()
        return allowed

    async def _destination(self, interaction: discord.Interaction) -> None:
        await self.session.begin_input(InputMode.DESTINATION, interaction)
        await interaction.response.send_message(
            "Send a channel mention or ID for a standalone message, or a Discord message link for a reply",
            ephemeral=True,
        )

    async def _content(self, interaction: discord.Interaction) -> None:
        await self.session.begin_input(InputMode.CONTENT, interaction)
        await interaction.response.send_message(
            "Send the exact text Bot Proxy should publish",
            ephemeral=True,
        )

    async def _identity(self, interaction: discord.Interaction) -> None:
        presets = await self.session.manager.store.list_characters(
            self.session.guild.id
        )
        await interaction.response.send_message(
            "Choose an identity",
            view=IdentityPickerView(self.session, presets),
            ephemeral=True,
        )

    async def _help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=bot_proxy_guide_embed(),
            allowed_mentions=_no_mentions(),
            ephemeral=True,
        )

    async def _persistent(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        destination = self.session.draft.destination
        if destination is None or destination.message_id is not None:
            await self.session.manager.private_feedback(
                interaction,
                "Persistent Messaging requires a standalone channel destination",
            )
            return
        self.session.persistent_messaging = not self.session.persistent_messaging
        await self.session.refresh()
        await interaction.delete_original_response()

    async def _confirmation(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.session.send_confirmation = not self.session.send_confirmation
        await self.session.refresh()
        await interaction.delete_original_response()

    async def _send(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.session.send_confirmation:
            await self.session.prepare_preview(interaction)
        else:
            await self.session.publish_immediately(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.session.finish(SessionStatus.CANCELLED)
        await interaction.delete_original_response()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await self.session.manager.handle_expected_or_reported_error(
            interaction,
            error,
            action=f"use Bot Proxy dashboard {type(item).__name__}",
            session=self.session,
        )


class ConfirmSendingView(discord.ui.View):
    def __init__(self, session: BotProxyWorkflowSession) -> None:
        super().__init__(timeout=None)
        self.session = session
        self._add("Confirm sending", self._confirm, primary=True)
        self._add("Go back", self._back)
        self._add("Cancel", self._cancel, danger=True)

    def _add(
        self,
        label: str,
        callback: Any,
        *,
        primary: bool = False,
        danger: bool = False,
    ) -> None:
        style = discord.ButtonStyle.secondary
        if primary:
            style = discord.ButtonStyle.primary
        if danger:
            style = discord.ButtonStyle.danger
        button = discord.ui.Button(label=label, style=style)
        button.callback = callback
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = await _interaction_is_authorized(
            self.session.opener_id,
            interaction,
            control="preview",
        )
        if allowed:
            self.session.touch()
        return allowed

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.session.confirm_publish(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.session.discard_preview()
        await interaction.delete_original_response()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.session.finish(SessionStatus.CANCELLED)
        await interaction.delete_original_response()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        await self.session.manager.handle_expected_or_reported_error(
            interaction,
            error,
            action=f"use Bot Proxy confirmation {type(item).__name__}",
            session=self.session,
        )


class BotProxyWorkflowSession:
    def __init__(
        self,
        manager: BotProxyWorkflowManager,
        *,
        active: ActiveSession,
        guild: discord.Guild,
        moderator: discord.Member,
        launcher: discord.Message,
        thread: discord.Thread,
        dashboard: discord.Message,
        draft: BotProxyDraft,
    ) -> None:
        self.manager = manager
        self.active = active
        self.guild = guild
        self.moderator = moderator
        self.launcher = launcher
        self.thread = thread
        self.dashboard = dashboard
        self.draft = draft
        self.input_mode: InputMode | None = None
        self._input_interaction: discord.Interaction | None = None
        self.persistent_messaging = False
        self.send_confirmation = True
        self.view = DashboardView(self)
        self._timeout_task: asyncio.Task[None] | None = None
        self._publish_lock = asyncio.Lock()
        self._publishing = False
        self._preview_draft: BotProxyDraft | None = None
        self._preview_message: discord.Message | None = None
        self._preview_control: discord.Message | None = None
        self._confirm_view: ConfirmSendingView | None = None
        self._terminal = BotProxySession(
            active=active,
            registry=manager.registry,
            store=manager.store,
            thread=thread,
            dashboard=dashboard,
        )

    @property
    def opener_id(self) -> int:
        return self.active.moderator_id

    def touch(self) -> None:
        if self._timeout_task is not None:
            self._timeout_task.cancel()
        self._timeout_task = asyncio.create_task(self._expire())

    async def _expire(self) -> None:
        try:
            await asyncio.sleep(SESSION_TIMEOUT_SECONDS)
            await self.finish(SessionStatus.TIMED_OUT)
        except asyncio.CancelledError:
            return
        except Exception as error:  # noqa: BLE001
            await self.manager.report_session_error(
                self,
                error,
                action="expire Bot Proxy session",
            )

    async def begin_input(
        self,
        mode: InputMode,
        interaction: discord.Interaction,
    ) -> None:
        await self._delete_input_prompt()
        self.input_mode = mode
        self._input_interaction = interaction

    def set_identity(self, identity: ProxyIdentity) -> None:
        if identity.kind is IdentityType.CHARACTER:
            self.ensure_character_allowed()
        self.draft.identity = identity

    def ensure_character_allowed(self) -> None:
        if (
            self.draft.destination is not None
            and self.draft.destination.message_id is not None
        ):
            raise WorkflowInputError("Characters cannot reply to an existing message")

    def set_character(self, preset: CharacterPreset) -> None:
        self.set_identity(ProxyIdentity(
            IdentityType.CHARACTER,
            display_name=preset.display_name,
            preset_name=preset.preset_name,
            avatar_bytes=preset.avatar_bytes,
            avatar_media_type=preset.avatar_media_type,
            avatar_sha256=preset.avatar_sha256,
        ))

    def render_embed(self) -> discord.Embed:
        destination = "Not set"
        if self.draft.destination is not None:
            if self.draft.destination.message_id is None:
                destination = f"<#{self.draft.destination.channel_id}>"
            else:
                destination = (
                    f"[Reply target](https://discord.com/channels/"
                    f"{self.draft.destination.guild_id}/"
                    f"{self.draft.destination.channel_id}/"
                    f"{self.draft.destination.message_id})"
                )
        content = self.draft.content or "Not set"
        if len(content) > CONTENT_PREVIEW_LENGTH:
            content = f"{content[: CONTENT_PREVIEW_LENGTH - 3]}..."
        identity = "Bot"
        if self.draft.identity.kind is IdentityType.CHARACTER:
            identity = self.draft.identity.display_name or "Invalid character"
        errors = self.draft.validation_errors()
        return discord.Embed(
            title="Bot Proxy",
            description=(
                f"Destination: {destination}\n"
                f"Identity: {identity}\n"
                f"Content:\n{content}\n\n"
                f"Status: {'Ready' if not errors else errors[0]}\n"
                "Use Help for button instructions"
            ),
            color=discord.Color.blue(),
        )

    async def refresh(self) -> None:
        self.touch()
        previous_view = self.view
        self.view = DashboardView(self)
        previous_view.stop()
        await self.dashboard.edit(
            content=None,
            embed=self.render_embed(),
            view=self.view,
            allowed_mentions=_no_mentions(),
        )

    async def handle_message(self, message: discord.Message) -> bool:
        if message.author.id != self.opener_id or self.input_mode is None:
            return False
        self.touch()
        mode = self.input_mode
        try:
            permissions = message.channel.permissions_for(message.author)
            if not permissions.manage_messages:
                await self.thread.send(
                    "You need Manage Messages permission",
                    allowed_mentions=_no_mentions(),
                )
                return True
            await self._apply_input(message, mode)
            self.input_mode = None
            if (
                mode is InputMode.CONTENT
                and not self.send_confirmation
                and not self.draft.validation_errors()
                and self._input_interaction is not None
            ):
                await self.publish_immediately(self._input_interaction)
            else:
                await self.refresh()
                await self._delete_input_prompt()
        except (WorkflowInputError, ValueError) as error:
            await self._show_input_error(str(error))
        finally:
            try:
                await message.delete()
            except Exception as error:  # noqa: BLE001
                await self.manager.report_session_error(
                    self,
                    error,
                    action="delete consumed Bot Proxy input",
                )
        return True

    async def _apply_input(
        self,
        message: discord.Message,
        mode: InputMode,
    ) -> None:
        if mode is InputMode.CONTENT:
            if not message.content.strip():
                raise WorkflowInputError("Content cannot be empty")
            if len(message.content) > MAX_PROXY_CONTENT_LENGTH:
                raise WorkflowInputError(
                    f"Content cannot exceed {MAX_PROXY_CONTENT_LENGTH} characters"
                )
            self.draft.content = message.content
            return
        if mode is InputMode.DESTINATION:
            destination = await self.manager.resolve_destination(
                self.guild,
                message.content.strip(),
            )
            if (
                destination.message_id is not None
                and self.draft.identity.kind is IdentityType.CHARACTER
            ):
                raise WorkflowInputError("Characters cannot reply to an existing message")
            self.draft.destination = destination
            if destination.message_id is not None:
                self.persistent_messaging = False
            return
        await self._apply_avatar_input(message)

    async def _show_input_error(self, content: str) -> None:
        if self._input_interaction is None:
            await self.thread.send(content, allowed_mentions=_no_mentions())
            return
        await self._input_interaction.edit_original_response(content=content, view=None)

    async def _delete_input_prompt(self) -> None:
        interaction = self._input_interaction
        self._input_interaction = None
        if interaction is None:
            return
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            return
        except Exception as error:  # noqa: BLE001
            await self.manager.report_session_error(
                self,
                error,
                action="clean up Bot Proxy input prompt",
            )

    async def _apply_avatar_input(self, message: discord.Message) -> None:
        if len(message.attachments) != 1:
            raise WorkflowInputError("Send exactly one image attachment")
        loaded = await self.manager.load_avatar_attachment(message.attachments[0])
        identity = self.draft.identity
        if identity.kind is not IdentityType.CHARACTER:
            raise WorkflowInputError("Choose a character before uploading an avatar")
        if identity.preset_name is None:
            self.draft.identity = ProxyIdentity(
                IdentityType.CHARACTER,
                display_name=identity.display_name,
                avatar_bytes=loaded.data,
                avatar_media_type=loaded.media_type,
                avatar_sha256=loaded.sha256,
            )
            return
        preset = await self.manager.store.get_character(
            self.guild.id,
            identity.preset_name,
        )
        if preset is None:
            raise WorkflowInputError("Character preset no longer exists")
        updated = await self.manager.store.update_character(
            guild_id=self.guild.id,
            preset_name=preset.preset_name,
            expected_revision=preset.revision,
            new_preset_name=preset.preset_name,
            display_name=preset.display_name,
            avatar_bytes=loaded.data,
            avatar_media_type=loaded.media_type,
            moderator_id=message.author.id,
        )
        self.set_character(updated)
        await self.manager.log_preset_change(
            self.guild,
            message.author,
            "updated",
            updated,
        )

    async def set_destination(self, destination: ProxyDestination) -> None:
        if (
            destination.message_id is not None
            and self.draft.identity.kind is IdentityType.CHARACTER
        ):
            raise WorkflowInputError("Characters cannot reply to an existing message")
        self.draft.destination = destination
        if destination.message_id is not None:
            self.persistent_messaging = False
        await self.refresh()
        await self.thread.send(
            f"{self.moderator.mention} destination updated",
            allowed_mentions=_moderator_mention(self.moderator),
        )

    async def prepare_preview(self, interaction: discord.Interaction) -> None:
        if self._publish_lock.locked():
            await self.manager.private_feedback(
                interaction,
                "Bot Proxy is already sending this message",
            )
            return
        async with self._publish_lock:
            errors = self.draft.validation_errors()
            if errors:
                await interaction.edit_original_response(content=errors[0])
                return
            draft = copy.deepcopy(self.draft)
            preview_message = None
            try:
                await self.manager.resolve_publish_channel(
                    self.guild,
                    draft.destination,
                    interaction.user,
                    identity_type=draft.identity.kind,
                )
                preview_message = await self.manager.publisher.preview(
                    draft=draft,
                    channel=self.thread,
                )
                confirm_view = ConfirmSendingView(self)
                control = await self.thread.send(
                    "Confirm this exact Bot Proxy message",
                    view=confirm_view,
                    allowed_mentions=_no_mentions(),
                )
            except Exception as error:  # noqa: BLE001
                if preview_message is not None:
                    try:
                        await preview_message.delete()
                    except Exception as cleanup_error:  # noqa: BLE001
                        await self.manager.report_session_error(
                            self,
                            cleanup_error,
                            action="clean up failed Bot Proxy preview",
                        )
                await self.manager.handle_expected_or_reported_error(
                    interaction,
                    error,
                    action="publish Bot Proxy message",
                    session=self,
                )
                return
            self._preview_draft = draft
            self._preview_message = preview_message
            self._preview_control = control
            self._confirm_view = confirm_view
            self._publishing = True
            await self.refresh()
            await interaction.delete_original_response()

    async def confirm_publish(self, interaction: discord.Interaction) -> None:
        async with self._publish_lock:
            if self._terminal.status is not None:
                await self.manager.private_feedback(
                    interaction,
                    "Bot Proxy session is already closed",
                )
                return
            draft = self._preview_draft
            if draft is None:
                await self.manager.private_feedback(interaction, "Preview is no longer active")
                return
            if not await self._publish_frozen(
                interaction,
                draft,
                action="publish confirmed Bot Proxy message",
            ):
                return
            await self._clear_preview_messages()
            await self._complete_publication(interaction)

    async def publish_immediately(self, interaction: discord.Interaction) -> None:
        if self._publish_lock.locked():
            await self.manager.private_feedback(
                interaction,
                "Bot Proxy is already sending this message",
            )
            return
        async with self._publish_lock:
            if self._terminal.status is not None:
                await self.manager.private_feedback(
                    interaction,
                    "Bot Proxy session is already closed",
                )
                return
            errors = self.draft.validation_errors()
            if errors:
                await self.manager.private_feedback(interaction, errors[0])
                return
            draft = copy.deepcopy(self.draft)
            self._publishing = True
            await self.refresh()
            if not await self._publish_frozen(
                interaction,
                draft,
                action="publish immediate Bot Proxy message",
            ):
                self._publishing = False
                await self.refresh()
                return
            await self._complete_publication(interaction)

    async def _publish_frozen(
        self,
        interaction: discord.Interaction,
        draft: BotProxyDraft,
        *,
        action: str,
    ) -> bool:
        try:
            channel = await self.manager.resolve_publish_channel(
                self.guild,
                draft.destination,
                interaction.user,
                identity_type=draft.identity.kind,
            )
            message = await self.manager.publisher.publish(
                draft=draft,
                moderator_id=interaction.user.id,
                channel=channel,
            )
        except Exception as error:  # noqa: BLE001
            await self.manager.handle_expected_or_reported_error(
                interaction,
                error,
                action=action,
                session=self,
            )
            return False
        await self.manager.log_publication(
            self,
            interaction.user,
            message,
            draft=draft,
        )
        return True

    async def _complete_publication(self, interaction: discord.Interaction) -> None:
        if self.persistent_messaging:
            self.draft.content = None
        else:
            self.draft = BotProxyDraft()
        self._publishing = False
        await self.refresh()
        if self._input_interaction is interaction:
            await self._delete_input_prompt()
        else:
            await interaction.delete_original_response()

    async def discard_preview(self) -> None:
        async with self._publish_lock:
            self._preview_draft = None
            await self._clear_preview_messages()
            self._publishing = False
            await self.refresh()

    async def _clear_preview_messages(self) -> None:
        self._preview_draft = None
        if self._confirm_view is not None:
            self._confirm_view.stop()
        messages = (self._preview_message, self._preview_control)
        self._preview_message = None
        self._preview_control = None
        self._confirm_view = None
        for message in messages:
            if message is None:
                continue
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except Exception as error:  # noqa: BLE001
                await self.manager.report_session_error(
                    self,
                    error,
                    action="clean up Bot Proxy preview",
                )

    async def finish(self, status: SessionStatus) -> None:
        async with self._publish_lock:
            if self._terminal.status is not None:
                return
            current_task = asyncio.current_task()
            if self._timeout_task is not None and self._timeout_task is not current_task:
                self._timeout_task.cancel()
            self._timeout_task = None
            await self._delete_input_prompt()
            await self._clear_preview_messages()
            self.view.stop()
            self.manager.sessions.pop(self.active.session_id, None)
            delete_closed = await self.manager.delete_closed_sessions(self.guild)
            if delete_closed:
                await self._terminal.finish(status, launcher=self.launcher, delete=True)
            else:
                await self._terminal.finish(status)


class EditTrackedMessageModal(discord.ui.Modal):
    def __init__(
        self,
        manager: BotProxyWorkflowManager,
        record: Any,
        owner_id: int,
    ) -> None:
        super().__init__(title="Edit Bot Proxy message")
        self.manager = manager
        self.record = record
        self.owner_id = owner_id
        self.content = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            default=record.content,
            required=True,
            max_length=MAX_PROXY_CONTENT_LENGTH,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _interaction_is_authorized(
            self.owner_id,
            interaction,
            control="action",
        ):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await self.manager.resolve_publish_channel(
                interaction.guild,
                ProxyDestination(self.record.guild_id, self.record.channel_id),
                interaction.user,
                identity_type=(
                    IdentityType.CHARACTER
                    if self.record.sender.value == IdentityType.CHARACTER.value
                    else IdentityType.BOT
                ),
            )
            updated = await self.manager.publisher.edit_tracked(
                record=self.record,
                channel=channel,
                content=str(self.content.value),
                moderator_id=interaction.user.id,
            )
            await self.manager.log_tracked_change(
                interaction.guild,
                interaction.user,
                "edited",
                updated,
            )
            await interaction.edit_original_response(
                content="Bot Proxy message edited",
                view=None,
            )
        except Exception as error:  # noqa: BLE001
            await self.manager.handle_tracked_error(
                interaction,
                error,
                action="edit Bot Proxy message",
                record=self.record,
            )


class ConfirmDeleteTrackedView(discord.ui.View):
    def __init__(
        self,
        manager: BotProxyWorkflowManager,
        record: Any,
        owner_id: int,
    ) -> None:
        super().__init__(timeout=30)
        self.manager = manager
        self.record = record
        self.owner_id = owner_id
        confirm = discord.ui.Button(
            label="Delete",
            style=discord.ButtonStyle.danger,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)
        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _interaction_is_authorized(
            self.owner_id,
            interaction,
            control="action",
        )

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await self.manager.resolve_publish_channel(
                interaction.guild,
                ProxyDestination(self.record.guild_id, self.record.channel_id),
                interaction.user,
                identity_type=(
                    IdentityType.CHARACTER
                    if self.record.sender.value == IdentityType.CHARACTER.value
                    else IdentityType.BOT
                ),
            )
            deleted = await self.manager.publisher.delete_tracked(
                record=self.record,
                channel=channel,
                moderator_id=interaction.user.id,
            )
            await self.manager.log_tracked_change(
                interaction.guild,
                interaction.user,
                "deleted",
                deleted,
            )
            await interaction.edit_original_response(
                content="Bot Proxy message deleted",
                view=None,
            )
        except Exception as error:  # noqa: BLE001
            await self.manager.handle_tracked_error(
                interaction,
                error,
                action="delete Bot Proxy message",
                record=self.record,
            )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled", view=None)


class TrackedMessageActionsView(discord.ui.View):
    def __init__(
        self,
        manager: BotProxyWorkflowManager,
        record: Any,
        owner_id: int,
    ) -> None:
        super().__init__(timeout=30)
        self.manager = manager
        self.record = record
        self.owner_id = owner_id
        for label, callback, style in (
            ("Use as destination", self._use, discord.ButtonStyle.primary),
            ("Edit", self._edit, discord.ButtonStyle.secondary),
            ("Delete", self._delete, discord.ButtonStyle.danger),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _interaction_is_authorized(
            self.owner_id,
            interaction,
            control="action",
        )

    async def _use(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.manager.route_destination_after_defer(
            interaction,
            ProxyDestination(
                self.record.guild_id,
                self.record.channel_id,
                self.record.message_id,
            ),
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            EditTrackedMessageModal(self.manager, self.record, self.owner_id)
        )

    async def _delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Delete this Bot Proxy message? This cannot be undone",
            view=ConfirmDeleteTrackedView(self.manager, self.record, self.owner_id),
        )


class SessionPickerView(discord.ui.View):
    def __init__(
        self,
        manager: BotProxyWorkflowManager,
        sessions: tuple[ActiveSession, ...],
        destination: ProxyDestination,
        *,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=30)
        self.manager = manager
        self.sessions = sessions
        self.destination = destination
        self.page = page
        start = page * PRESETS_PER_PAGE
        page_sessions = sessions[start : start + PRESETS_PER_PAGE]
        options = [
            discord.SelectOption(
                label=f"Bot Proxy {start + index + 1}",
                value=session.session_id,
                description=f"Thread {session.thread_id}",
            )
            for index, session in enumerate(page_sessions)
        ]
        select = discord.ui.Select(
            placeholder="Choose session",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._select
        self.add_item(select)
        previous = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            disabled=page == 0,
            row=1,
        )
        previous.callback = self._previous
        self.add_item(previous)
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=start + PRESETS_PER_PAGE >= len(sessions),
            row=1,
        )
        next_button.callback = self._next
        self.add_item(next_button)

    async def _select(self, interaction: discord.Interaction) -> None:
        session_id = interaction.data["values"][0]
        session = self.manager.sessions.get(session_id)
        if session is None:
            await interaction.response.edit_message(
                content="This Bot Proxy session is no longer active",
                view=None,
            )
            return
        if interaction.user.id != session.opener_id:
            await interaction.response.send_message(
                "Only the session owner can select it",
                ephemeral=True,
            )
            return
        try:
            await session.set_destination(self.destination)
        except WorkflowInputError as error:
            await interaction.response.edit_message(content=str(error), view=None)
            return
        await interaction.response.edit_message(
            content=f"Open {session.thread.mention}",
            view=None,
        )

    async def _previous(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=SessionPickerView(
                self.manager,
                self.sessions,
                self.destination,
                page=max(0, self.page - 1),
            )
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=SessionPickerView(
                self.manager,
                self.sessions,
                self.destination,
                page=self.page + 1,
            )
        )
