"""Manual evidence collection and moderator-selected punishments."""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import discord
from redbot.core import modlog

from .settings import GuildSettings

MEMENT_ROLE_ID = 803692340749140008
MAX_MUTE_SECONDS = 28 * 24 * 60 * 60
PREVIEW_LENGTH = 1200
DISCORD_MESSAGE_LIMIT = 2_000
MAX_REASON_LENGTH = 500
DETAILS_TIMEOUT_SECONDS = 300


class MemberAction(str, Enum):
    NONE = "none"
    MUTE = "mute"
    KICK = "kick"
    BAN = "ban"


@dataclass
class EvidenceSelection:
    mement: bool = False
    member_action: MemberAction = MemberAction.NONE

    @property
    def mute(self) -> bool:
        return self.member_action is MemberAction.MUTE

    def toggle_mement(self) -> None:
        self.mement = not self.mement
        if self.mement and self.member_action in (MemberAction.KICK, MemberAction.BAN):
            self.member_action = MemberAction.NONE

    def select_member_action(self, action: str) -> None:
        self.member_action = MemberAction(action)
        if self.member_action in (MemberAction.KICK, MemberAction.BAN):
            self.mement = False


@dataclass(frozen=True)
class PublishedEvidence:
    primary: Any
    parts: tuple[Any, ...]
    content_external: bool


@dataclass
class PreliminaryResult:
    source_message: discord.Message
    moderator: Any
    selection: EvidenceSelection
    settings: GuildSettings
    evidence: PublishedEvidence
    outcomes: list[str]
    member: Any | None = None


@dataclass(frozen=True)
class PreliminaryContext:
    settings: GuildSettings
    evidence_channel: Any
    member: Any | None


def parse_mute_duration(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([mhdw])", value.strip().lower())
    if match is None:
        raise ValueError("Use a duration such as 30m, 2h, 3d, or 1w.")
    amount = int(match.group(1))
    multiplier = {
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60,
    }[match.group(2)]
    duration = amount * multiplier
    if duration > MAX_MUTE_SECONDS:
        raise ValueError("Mute duration must not exceed 28 days.")
    return duration


class PunishmentDetailsSession:
    def __init__(
        self,
        controller: Any,
        interaction: discord.Interaction,
        source_message: discord.Message,
        selection: EvidenceSelection,
    ) -> None:
        self.controller = controller
        self.interaction = interaction
        self.source_message = source_message
        self.selection = selection
        self.moderator_id = interaction.user.id
        self.preliminary_task: asyncio.Task[PreliminaryResult | None] | None = None
        self.timeout_task: asyncio.Task[Any] | None = None
        self.recovery_message: Any | None = None
        self._active = True
        self._lock = asyncio.Lock()
        self.recovery_view = PunishmentDetailsRecoveryView(self)

    @property
    def active(self) -> bool:
        return self._active

    def create_modal(
        self,
        *,
        title: str = "Punishment details",
        defaults: dict[str, str] | None = None,
    ) -> PunishmentDetailsModal:
        return PunishmentDetailsModal(self, title=title, defaults=defaults)

    def start(self, preliminary_task: asyncio.Task[PreliminaryResult | None]) -> None:
        self.preliminary_task = preliminary_task
        timeout_task = asyncio.create_task(self._wait_for_deadline())
        self.timeout_task = timeout_task
        self.controller._own_task(timeout_task, "manual evidence details timeout")

    async def send_modal(
        self,
        interaction: discord.Interaction,
        *,
        title: str = "Punishment details",
        defaults: dict[str, str] | None = None,
    ) -> bool:
        async with self._lock:
            if not self._active:
                return False
            await interaction.response.send_modal(self.create_modal(title=title, defaults=defaults))
            return True

    async def claim(self) -> bool:
        async with self._lock:
            if not self._active:
                return False
            self._active = False
            self._cancel_timeout()
        await self._disable_recovery()
        return True

    async def expire(self) -> bool:
        async with self._lock:
            if not self._active:
                return False
            self._active = False
            self._cancel_timeout()
        await self._disable_recovery()
        await self.controller.expire_details(self)
        return True

    async def attach_recovery_message(self, message: Any) -> None:
        self.recovery_message = message
        if not self._active:
            await self._disable_recovery()

    def _cancel_timeout(self) -> None:
        task = self.timeout_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _disable_recovery(self) -> None:
        button = self.recovery_view.open_button
        button.disabled = True
        message = self.recovery_message
        if message is None:
            return
        try:
            await message.edit(view=self.recovery_view)
        except discord.HTTPException:
            pass

    async def _wait_for_deadline(self) -> None:
        await asyncio.sleep(DETAILS_TIMEOUT_SECONDS)
        await self.expire()


class PunishmentDetailsModal(discord.ui.Modal):
    def __init__(
        self,
        session: PunishmentDetailsSession,
        *,
        title: str = "Punishment details",
        defaults: dict[str, str] | None = None,
    ) -> None:
        super().__init__(title=title, timeout=300)
        self.session = session
        self.controller = session.controller
        self.source_message = session.source_message
        self.selection = session.selection
        self.inputs: dict[str, discord.ui.TextInput] = {}
        self.defaults = defaults or {}

        if self.selection.mement:
            self._add_reason("mement_reason", "Memen't reason")
        if self.selection.mute:
            duration = discord.ui.TextInput(
                label="Mute duration",
                placeholder="30m, 2h, 3d, or 1w",
                required=True,
                max_length=8,
                default=self.defaults.get("mute_duration"),
            )
            self.inputs["mute_duration"] = duration
            self.add_item(duration)
            self._add_reason("mute_reason", "Mute reason")
        if self.selection.member_action is MemberAction.KICK:
            self._add_reason("member_action_reason", "Kick reason")
        elif self.selection.member_action is MemberAction.BAN:
            self._add_reason("member_action_reason", "Ban reason")

    def _add_reason(self, name: str, label: str) -> None:
        field = discord.ui.TextInput(
            label=label,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=MAX_REASON_LENGTH,
            default=self.defaults.get(name),
        )
        self.inputs[name] = field
        self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.controller.submit_details(interaction, self)


class PunishmentDetailsRecoveryView(discord.ui.View):
    def __init__(self, session: PunishmentDetailsSession) -> None:
        super().__init__(timeout=DETAILS_TIMEOUT_SECONDS)
        self.session = session
        self.open_button = discord.ui.Button(
            label="Enter punishment details",
            style=discord.ButtonStyle.primary,
        )
        self.open_button.callback = self._open
        self.add_item(self.open_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.session.moderator_id:
            return True
        await interaction.response.send_message(
            "Only the moderator who started this action can enter its details.",
            ephemeral=True,
        )
        return False

    async def _open(self, interaction: discord.Interaction) -> None:
        if not await self.session.send_modal(interaction):
            await interaction.response.send_message(
                "Punishment details are no longer active.",
                ephemeral=True,
            )

    async def on_timeout(self) -> None:
        await self.session.expire()


class EvidenceActionView(discord.ui.View):
    def __init__(
        self,
        controller: Any,
        source_message: discord.Message,
        *,
        moderator_id: int,
        allow_mement: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.controller = controller
        self.source_message = source_message
        self.moderator_id = moderator_id
        self.selection = EvidenceSelection()
        self.mement_button: discord.ui.Button | None = None

        if allow_mement:
            self.mement_button = discord.ui.Button(
                label="Memen't: Off",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            self.mement_button.callback = self._toggle_mement
            self.add_item(self.mement_button)

        self.member_action = discord.ui.Select(
            placeholder="Member action: None",
            options=self._member_action_options(),
            row=1,
        )
        self.member_action.callback = self._select_member_action
        self.add_item(self.member_action)

        self.confirm_button = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.danger,
            row=2,
        )
        self.confirm_button.callback = self._confirm
        self.add_item(self.confirm_button)

        self.cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        self.cancel_button.callback = self._cancel
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        permissions = interaction.permissions
        if (
            interaction.user.id == self.moderator_id
            and permissions is not None
            and permissions.manage_messages
        ):
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this panel can use it while they "
            "have Manage Messages permission.",
            ephemeral=True,
        )
        return False

    def _member_action_options(self) -> list[discord.SelectOption]:
        selected = self.selection.member_action
        return [
            discord.SelectOption(
                label="None",
                value=MemberAction.NONE.value,
                default=selected is MemberAction.NONE,
            ),
            discord.SelectOption(
                label="Mute",
                value=MemberAction.MUTE.value,
                default=selected is MemberAction.MUTE,
            ),
            discord.SelectOption(
                label="Kick",
                value=MemberAction.KICK.value,
                default=selected is MemberAction.KICK,
            ),
            discord.SelectOption(
                label="Ban",
                value=MemberAction.BAN.value,
                default=selected is MemberAction.BAN,
            ),
        ]

    def _refresh(self) -> None:
        if self.mement_button is not None:
            self.mement_button.label = "Memen't: On" if self.selection.mement else "Memen't: Off"
            self.mement_button.style = (
                discord.ButtonStyle.success
                if self.selection.mement
                else discord.ButtonStyle.secondary
            )
        self.member_action.placeholder = (
            f"Member action: {self.selection.member_action.value.title()}"
        )
        self.member_action.options = self._member_action_options()

    async def _toggle_mement(self, interaction: discord.Interaction) -> None:
        self.selection.toggle_mement()
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _select_member_action(self, interaction: discord.Interaction) -> None:
        self.selection.select_member_action(self.member_action.values[0])
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.controller.confirm(interaction, self)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Manual evidence action cancelled.",
            embed=None,
            view=None,
        )


class ManualEvidenceController:
    def __init__(self, cog: Any) -> None:
        self.cog = cog
        self._tasks: set[asyncio.Task[Any]] = set()
        self._task_labels: dict[asyncio.Task[Any], str] = {}
        self.context_menu = discord.app_commands.ContextMenu(
            name="Add evidence",
            callback=self.open,
        )
        self.context_menu.default_permissions = discord.Permissions(manage_messages=True)
        self.context_menu.guild_only = True
        self._registered = False

    def register(self) -> None:
        command_type = discord.AppCommandType.message
        existing = self.cog.bot.tree.get_command(
            self.context_menu.name,
            type=command_type,
        )
        if existing is self.context_menu:
            self._registered = True
            return
        self.cog.bot.tree.add_command(self.context_menu, override=True)
        self._registered = True

    def unregister(self) -> None:
        if not self._registered:
            return
        command_type = discord.AppCommandType.message
        existing = self.cog.bot.tree.get_command(
            self.context_menu.name,
            type=command_type,
        )
        if existing is self.context_menu:
            self.cog.bot.tree.remove_command(
                self.context_menu.name,
                type=command_type,
            )
        self._registered = False

    async def shutdown(self) -> None:
        self.unregister()
        pending = tuple(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._task_labels.clear()

    async def confirm(
        self,
        interaction: discord.Interaction,
        view: EvidenceActionView,
    ) -> None:
        selection = EvidenceSelection(
            mement=view.selection.mement,
            member_action=view.selection.member_action,
        )
        view.stop()
        session = None
        if selection.mement or selection.member_action is not MemberAction.NONE:
            session = PunishmentDetailsSession(
                self,
                interaction,
                view.source_message,
                selection,
            )
            await session.send_modal(interaction)
        else:
            await interaction.response.defer(ephemeral=True)

        task = asyncio.create_task(
            self._run_preliminary(
                interaction,
                view.source_message,
                selection,
                session,
            )
        )
        self._own_task(task, "manual evidence action")
        if session is not None:
            session.start(task)

    def _own_task(self, task: asyncio.Task[Any], label: str) -> None:
        self._tasks.add(task)
        self._task_labels[task] = label
        task.add_done_callback(self._finish_task)

    def _finish_task(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        label = self._task_labels.pop(task, "manual evidence action")
        self.cog._observe_background_task(task, label)

    async def _run_preliminary(
        self,
        interaction: discord.Interaction,
        source_message: discord.Message,
        selection: EvidenceSelection,
        session: PunishmentDetailsSession | None = None,
    ) -> PreliminaryResult | None:
        guild = source_message.guild
        prepared = await self._prepare_preliminary(
            interaction,
            source_message,
            selection,
        )
        if prepared is None:
            return None
        guild_settings = prepared.settings
        evidence_channel = prepared.evidence_channel
        member = prepared.member

        try:
            evidence = await _publish_evidence(
                evidence_channel,
                source_message,
                interaction.user,
                selection,
            )
        except (discord.HTTPException, OSError):
            await interaction.followup.send(
                "Evidence could not be saved. The source message was not deleted.",
                ephemeral=True,
            )
            return None

        try:
            await source_message.delete()
        except discord.HTTPException:
            await interaction.followup.send(
                "Evidence was saved, but the source message could not be deleted.\n"
                f"{evidence.primary.jump_url}",
                ephemeral=True,
            )
            return None

        outcomes = _initial_action_outcomes(selection)
        if selection.mement:
            await self._apply_preliminary_mement(
                guild,
                member,
                interaction.user,
                outcomes,
            )

        result = PreliminaryResult(
            source_message=source_message,
            moderator=interaction.user,
            selection=selection,
            settings=guild_settings,
            evidence=evidence,
            outcomes=outcomes,
            member=member,
        )
        try:
            await _update_evidence(result)
        except discord.HTTPException:
            await self._send_preliminary_followup(
                interaction,
                "Evidence was saved and the source message was deleted, but "
                "the evidence summary could not be updated.\n"
                f"{evidence.primary.jump_url}",
                session,
            )
        else:
            await self._send_preliminary_followup(
                interaction,
                f"Evidence saved and the source message was deleted.\n{evidence.primary.jump_url}",
                session,
            )
        return result

    @staticmethod
    async def _send_preliminary_followup(
        interaction: discord.Interaction,
        content: str,
        session: PunishmentDetailsSession | None,
    ) -> None:
        if session is None or not session.active:
            await interaction.followup.send(content, ephemeral=True)
            return
        message = await interaction.followup.send(
            content,
            ephemeral=True,
            view=session.recovery_view,
            wait=True,
        )
        await session.attach_recovery_message(message)

    async def _prepare_preliminary(
        self,
        interaction: discord.Interaction,
        source_message: discord.Message,
        selection: EvidenceSelection,
    ) -> PreliminaryContext | None:
        guild = source_message.guild
        member = await _resolve_source_member(guild, source_message.author)
        selected_punishment = selection.mement or selection.member_action is not MemberAction.NONE
        if selected_punishment and member is None:
            await interaction.followup.send(
                "The source author is no longer available as a server member. "
                "The source message was not deleted.",
                ephemeral=True,
            )
            return None
        protected_check = getattr(self.cog, "_is_protected_member", None)
        if (
            selected_punishment
            and member is not None
            and callable(protected_check)
            and await protected_check(member, guild)
        ):
            await interaction.followup.send(
                "The source author is protected from moderation actions. "
                "The source message was not deleted.",
                ephemeral=True,
            )
            return None

        raw_config = await self.cog.config.guild(guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        evidence_channel = (
            guild.get_channel(guild_settings.manual_evidence_channel)
            if guild_settings.manual_evidence_channel is not None
            else None
        )
        if evidence_channel is None:
            await interaction.followup.send(
                "Evidence could not be saved. The source message was not deleted.",
                ephemeral=True,
            )
            return None

        permission_check = getattr(self.cog, "_missing_channel_permissions", None)
        if callable(permission_check):
            missing = permission_check(
                guild,
                evidence_channel,
                read_history=True,
                embed_links=True,
                attach_files=True,
            )
            if missing is None:
                missing = permission_check(
                    guild,
                    source_message.channel,
                    send_messages=False,
                    manage_messages=True,
                )
            if missing is not None:
                await interaction.followup.send(missing, ephemeral=True)
                return None
        return PreliminaryContext(guild_settings, evidence_channel, member)

    @staticmethod
    async def _apply_preliminary_mement(
        guild: discord.Guild,
        member: Any,
        moderator: Any,
        outcomes: list[str],
    ) -> None:
        role = guild.get_role(MEMENT_ROLE_ID)
        if role is None:
            _set_action_outcome(
                outcomes,
                "Memen't:",
                "Memen't: Failed because the role was not found",
            )
            return
        try:
            await member.add_roles(
                role,
                reason=f"Manual evidence action by {moderator} ({moderator.id})",
            )
        except discord.HTTPException:
            _set_action_outcome(
                outcomes,
                "Memen't:",
                "Memen't: Failed to apply",
            )
        else:
            _set_action_outcome(
                outcomes,
                "Memen't:",
                "Memen't: Applied",
            )

    async def submit_details(
        self,
        interaction: discord.Interaction,
        modal: PunishmentDetailsModal,
    ) -> None:
        duration_label = ""
        duration_seconds = 0
        if modal.selection.mute:
            duration_label = modal.inputs["mute_duration"].value.strip().lower()
            try:
                duration_seconds = parse_mute_duration(duration_label)
            except ValueError:
                defaults = {name: field.value for name, field in modal.inputs.items()}
                reopened = await modal.session.send_modal(
                    interaction,
                    title="Invalid mute duration. Try again",
                    defaults=defaults,
                )
                if not reopened:
                    await interaction.response.send_message(
                        "Punishment details are no longer active.",
                        ephemeral=True,
                    )
                return

        if not await modal.session.claim():
            await interaction.response.send_message(
                "Punishment details are no longer active.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        preliminary_task = modal.session.preliminary_task
        if preliminary_task is None:
            await interaction.followup.send(
                "Punishments were not applied because evidence collection did not start.",
                ephemeral=True,
            )
            return
        preliminary = await preliminary_task
        if preliminary is None:
            await interaction.followup.send(
                "Punishments were not applied because evidence collection failed.",
                ephemeral=True,
            )
            return

        independent_actions = []
        if modal.selection.mement:
            reason = modal.inputs["mement_reason"].value.strip()
            preliminary.outcomes.append(f"Memen't reason: {reason}")
            independent_actions.append(self._send_mement_notification(preliminary, reason))

        if modal.selection.mute:
            reason = modal.inputs["mute_reason"].value.strip()
            independent_actions.append(
                self._apply_mute(
                    preliminary,
                    duration_label,
                    duration_seconds,
                    reason,
                )
            )

        if independent_actions:
            await asyncio.gather(*independent_actions)

        if modal.selection.member_action in (MemberAction.KICK, MemberAction.BAN):
            reason = modal.inputs["member_action_reason"].value.strip()
            await self._apply_member_action(preliminary, reason)

        try:
            await _update_evidence(preliminary)
        except discord.HTTPException:
            await interaction.followup.send(
                "Punishments were processed, but the evidence summary could not be updated.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Selected punishments were processed.\n{preliminary.evidence.primary.jump_url}",
            ephemeral=True,
        )

    async def expire_details(self, session: PunishmentDetailsSession) -> None:
        preliminary_task = session.preliminary_task
        if preliminary_task is None:
            return
        preliminary = await preliminary_task
        if preliminary is None:
            return

        selection = session.selection
        if selection.mement and "Memen't: Applied" in preliminary.outcomes:
            _set_action_outcome(
                preliminary.outcomes,
                "Memen't notification:",
                "Memen't notification: Cancelled because no reason was submitted",
            )
        if selection.member_action is not MemberAction.NONE:
            action = selection.member_action.value.title()
            _set_action_outcome(
                preliminary.outcomes,
                f"{action}:",
                f"{action}: Cancelled because details were not submitted",
            )
        try:
            await _update_evidence(preliminary)
        except discord.HTTPException:
            await session.interaction.followup.send(
                "Punishment details expired, but the evidence summary could not be updated.",
                ephemeral=True,
            )
            return
        await session.interaction.followup.send(
            "Punishment details expired. No pending punishments were applied.\n"
            f"{preliminary.evidence.primary.jump_url}",
            ephemeral=True,
        )

    async def _send_mement_notification(
        self,
        result: PreliminaryResult,
        reason: str,
    ) -> None:
        if "Memen't: Applied" not in result.outcomes:
            result.outcomes.append("Memen't notification: Not sent")
            return
        channel_id = result.settings.manual_evidence_mement_notification_channel
        channel = (
            result.source_message.guild.get_channel(channel_id) if channel_id is not None else None
        )
        if channel is None:
            result.outcomes.append("Memen't notification: Channel not configured")
            return
        target = result.member or result.source_message.author
        moderator = result.moderator
        try:
            await channel.send(
                f"<@{target.id}> received memen't from <@{moderator.id}>.\nReason: {reason}",
                allowed_mentions=_target_only_mentions(target),
            )
        except discord.HTTPException:
            result.outcomes.append("Memen't notification: Failed")
        else:
            result.outcomes.append("Memen't notification: Sent")

    async def _apply_mute(
        self,
        result: PreliminaryResult,
        duration_label: str,
        duration_seconds: int,
        reason: str,
    ) -> None:
        result.outcomes.extend(
            (
                f"Mute duration: {duration_label}",
                f"Mute reason: {reason}",
            )
        )
        mutes = self.cog.bot.get_cog("Mutes")
        if mutes is None:
            _set_action_outcome(
                result.outcomes,
                "Mute:",
                "Mute: Failed because the Mutes cog is unavailable",
            )
            return
        source = result.source_message
        target = result.member or source.author
        mute_config = getattr(mutes, "config", None)
        if mute_config is None:
            _set_action_outcome(
                result.outcomes,
                "Mute:",
                "Mute: Failed because its role is not configured",
            )
            return
        role_id = await mute_config.guild(source.guild).mute_role()
        if not role_id or source.guild.get_role(role_id) is None:
            _set_action_outcome(
                result.outcomes,
                "Mute:",
                "Mute: Failed because its role is not configured",
            )
            return
        until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
        try:
            response = await mutes.mute_user(
                source.guild,
                result.moderator,
                target,
                until=until,
                reason=reason,
            )
        except discord.HTTPException:
            _set_action_outcome(result.outcomes, "Mute:", "Mute: Failed")
            return
        if not getattr(response, "success", False):
            _set_action_outcome(
                result.outcomes,
                "Mute:",
                "Mute: Rejected by the Mutes cog",
            )
            return

        _set_action_outcome(
            result.outcomes,
            "Mute:",
            f"Mute: Applied for {duration_label}",
        )
        try:
            await modlog.create_case(
                self.cog.bot,
                source.guild,
                source.created_at,
                "smute",
                target,
                result.moderator,
                reason,
                until=until,
                channel=None,
            )
        except Exception:
            result.outcomes.append("Mute ModLog case: Failed")

        try:
            await source.channel.send(
                f"<@{target.id}> was muted by <@{result.moderator.id}> "
                f"for {duration_label}.\nReason: {reason}",
                allowed_mentions=_moderation_mentions(
                    target,
                    result.moderator,
                ),
            )
        except discord.HTTPException:
            result.outcomes.append("Mute notification: Failed")
        else:
            result.outcomes.append("Mute notification: Sent")

    async def _apply_member_action(
        self,
        result: PreliminaryResult,
        reason: str,
    ) -> None:
        action = result.selection.member_action.value
        result.outcomes.append(f"{action.title()} reason: {reason}")
        action_result = await self.cog._execute_action(
            result.source_message.guild,
            result.member or result.source_message.author,
            result.source_message.created_at,
            result.settings,
            reason=reason,
            action=action,
            moderator=result.moderator,
        )
        label = getattr(action_result, "label", None)
        if label:
            _set_action_outcome(
                result.outcomes,
                f"{action.title()}:",
                f"{action.title()}: {label}",
            )
        else:
            _set_action_outcome(
                result.outcomes,
                f"{action.title()}:",
                f"{action.title()}: Failed",
            )

    async def open(
        self,
        interaction: discord.Interaction,
        source_message: discord.Message,
    ) -> None:
        guild = interaction.guild
        permissions = interaction.permissions
        if guild is None or permissions is None or not permissions.manage_messages:
            await interaction.response.send_message(
                "You need Manage Messages permission.",
                ephemeral=True,
            )
            return

        raw_config = await self.cog.config.guild(guild).all()
        settings = GuildSettings.from_mapping(raw_config)
        evidence_channel = (
            guild.get_channel(settings.manual_evidence_channel)
            if settings.manual_evidence_channel is not None
            else None
        )
        if evidence_channel is None:
            await interaction.response.send_message(
                "The manual evidence channel is not configured.",
                ephemeral=True,
            )
            return
        if evidence_channel.id == source_message.channel.id:
            await interaction.response.send_message(
                "Messages in the evidence channel cannot be added as evidence.",
                ephemeral=True,
            )
            return
        if not self.cog._channel_is_private(guild, evidence_channel):
            await interaction.response.send_message(
                "The manual evidence channel must be private.",
                ephemeral=True,
            )
            return
        missing = self.cog._missing_channel_permissions(
            guild,
            evidence_channel,
            read_history=True,
            embed_links=True,
            attach_files=True,
        )
        if missing is None:
            missing = self.cog._missing_channel_permissions(
                guild,
                source_message.channel,
                send_messages=False,
                manage_messages=True,
            )
        if missing is not None:
            await interaction.response.send_message(missing, ephemeral=True)
            return

        allow_mement = (
            settings.manual_evidence_memes_channel == source_message.channel.id
            and guild.get_role(MEMENT_ROLE_ID) is not None
        )
        preview = source_message.content or "[No text content]"
        if len(preview) > PREVIEW_LENGTH:
            preview = f"{preview[: PREVIEW_LENGTH - 3]}..."
        view = EvidenceActionView(
            self,
            source_message,
            moderator_id=interaction.user.id,
            allow_mement=allow_mement,
        )
        await interaction.response.send_message(
            (
                f"Add this message as evidence?\n\n"
                f"Author: {source_message.author.display_name} "
                f"({source_message.author.id})\n"
                f"Channel: {source_message.channel.mention}\n\n"
                f"{preview}"
            ),
            ephemeral=True,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def _publish_evidence(
    evidence_channel: Any,
    source_message: discord.Message,
    moderator: Any,
    selection: EvidenceSelection,
) -> PublishedEvidence:
    published = []
    rendered = _render_evidence(
        source_message,
        moderator,
        selection,
        source_deletion="Pending",
    )
    files = []
    content_external = len(rendered) + _evidence_result_reserve(selection) > DISCORD_MESSAGE_LIMIT
    if content_external:
        files.append(
            discord.File(
                io.BytesIO(source_message.content.encode("utf-8")),
                filename="message.txt",
            )
        )
        rendered = _render_evidence(
            source_message,
            moderator,
            selection,
            source_deletion="Pending",
            content_override="[Stored in message.txt]",
        )
    primary = await evidence_channel.send(
        content=rendered,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    published.append(primary)
    try:
        files.extend(
            [await attachment.to_file(use_cached=True) for attachment in source_message.attachments]
        )
        for start in range(0, len(files), 10):
            part = await evidence_channel.send(
                content=f"Evidence files for source message {source_message.id}.",
                files=files[start : start + 10],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            published.append(part)
    except (discord.HTTPException, OSError):
        for message in reversed(published):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        raise
    return PublishedEvidence(
        primary=primary,
        parts=tuple(published[1:]),
        content_external=content_external,
    )


async def _update_evidence(result: PreliminaryResult) -> None:
    await result.evidence.primary.edit(
        content=_render_evidence(
            result.source_message,
            result.moderator,
            result.selection,
            source_deletion="Completed",
            content_override=(
                "[Stored in message.txt]" if result.evidence.content_external else None
            ),
            outcomes=result.outcomes,
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _resolve_source_member(guild: Any, author: Any) -> Any | None:
    get_member = getattr(guild, "get_member", None)
    member = get_member(author.id) if callable(get_member) else None
    if member is not None:
        return member
    fetch_member = getattr(guild, "fetch_member", None)
    if not callable(fetch_member):
        return None
    try:
        return await fetch_member(author.id)
    except (discord.NotFound, discord.HTTPException):
        return None


def _moderation_mentions(target: Any, moderator: Any) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[target, moderator],
        replied_user=False,
    )


def _target_only_mentions(target: Any) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[target],
        replied_user=False,
    )


def _initial_action_outcomes(selection: EvidenceSelection) -> list[str]:
    outcomes = []
    if selection.mement:
        outcomes.append("Memen't: Pending")
    if selection.member_action is not MemberAction.NONE:
        outcomes.append(f"{selection.member_action.value.title()}: Waiting for details")
    return outcomes


def _evidence_result_reserve(selection: EvidenceSelection) -> int:
    reason_count = int(selection.mement)
    if selection.member_action is not MemberAction.NONE:
        reason_count += 1
    return reason_count * (MAX_REASON_LENGTH + 150)


def _set_action_outcome(outcomes: list[str], prefix: str, value: str) -> None:
    for index, outcome in enumerate(outcomes):
        if outcome.startswith(prefix):
            outcomes[index] = value
            return
    outcomes.append(value)


async def show_status(cog: Any, ctx: Any) -> None:
    if not cog._group_overview_is_private(ctx):
        await ctx.send("Run this command in a private staff channel.")
        return
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    role = ctx.guild.get_role(MEMENT_ROLE_ID)
    await ctx.send(
        "Manual evidence settings\n"
        "Evidence channel: "
        f"{_channel_label(ctx.guild, guild_settings.manual_evidence_channel)}\n"
        f"Memes channel: {_channel_label(ctx.guild, guild_settings.manual_evidence_memes_channel)}\n"
        "Memen't notification channel: "
        f"{_channel_label(ctx.guild, guild_settings.manual_evidence_mement_notification_channel)}\n"
        f"Memen't role: {role.mention if role is not None else 'not found'}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _channel_label(guild: discord.Guild, channel_id: int | None) -> str:
    if channel_id is None:
        return "not set"
    channel = guild.get_channel(channel_id)
    return channel.mention if channel is not None else f"missing ({channel_id})"


def _render_evidence(
    source_message: discord.Message,
    moderator: Any,
    selection: EvidenceSelection,
    *,
    source_deletion: str,
    content_override: str | None = None,
    outcomes: list[str] | None = None,
) -> str:
    content = content_override or source_message.content or "[No text content]"
    selected = []
    if selection.mement:
        selected.append("Memen't")
    if selection.member_action is not MemberAction.NONE:
        selected.append(selection.member_action.value.title())
    action_label = ", ".join(selected) if selected else "None"
    timestamp = int(source_message.created_at.timestamp())
    rendered_outcomes = _initial_action_outcomes(selection) if outcomes is None else outcomes
    outcome_section = ""
    if rendered_outcomes:
        outcome_section = "\n\n**Action results**\n" + "\n".join(rendered_outcomes)
    return (
        "**Manual evidence**\n"
        f"Author: {source_message.author.display_name} ({source_message.author.id})\n"
        f"Moderator: {moderator.display_name} ({moderator.id})\n"
        f"Source: {source_message.channel.mention} ({source_message.channel.id})\n"
        f"Created: <t:{timestamp}:F>\n"
        f"Source message ID: {source_message.id}\n"
        f"Selected actions: {action_label}\n"
        f"Source deletion: {source_deletion}\n\n"
        f"**Content**\n{content}"
        f"{outcome_section}"
    )


async def clear_deleted_channel(cog: Any, channel: Any) -> None:
    guild_config = cog.config.guild(channel.guild)
    for name in (
        "manual_evidence_channel",
        "manual_evidence_memes_channel",
        "manual_evidence_mement_notification_channel",
    ):
        config_value = getattr(guild_config, name, None)
        if config_value is None:
            continue
        if await config_value() == channel.id:
            await config_value.clear()
