"""Moderator-selected message punishments and their configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import discord
from redbot.core import commands, modlog
from redbot.core.utils.chat_formatting import pagify

from . import manual_punishment_publication as publication
from .channel_routing import channel_scope_id
from .effects import EffectStatus, ModerationOrigin
from .settings import (
    MAX_MANUAL_PUNISHMENT_ROLES_PER_CHANNEL,
    GuildSettings,
    ManualPunishmentRoleSettings,
)

MAX_MUTE_SECONDS = 28 * 24 * 60 * 60
PREVIEW_LENGTH = 1200
MAX_REASON_LENGTH = publication.MAX_REASON_LENGTH


class MemberAction(str, Enum):
    NONE = "none"
    MUTE = "mute"
    KICK = "kick"
    BAN = "ban"


@dataclass
class PunishmentSelection:
    capture_evidence: bool = True
    member_action: MemberAction = MemberAction.NONE
    role_ids: tuple[int, ...] = ()

    @property
    def mute(self) -> bool:
        return self.member_action is MemberAction.MUTE

    @property
    def has_punishment(self) -> bool:
        return self.member_action is not MemberAction.NONE or bool(self.role_ids)

    def toggle_evidence(self) -> None:
        self.capture_evidence = not self.capture_evidence

    def select_member_action(self, action: str) -> None:
        self.member_action = MemberAction(action)
        if self.member_action in (MemberAction.KICK, MemberAction.BAN):
            self.role_ids = ()

    def select_roles(self, role_ids: tuple[int, ...]) -> None:
        self.role_ids = tuple(dict.fromkeys(role_ids))
        if self.role_ids and self.member_action in (MemberAction.KICK, MemberAction.BAN):
            self.member_action = MemberAction.NONE


@dataclass(frozen=True)
class PreparedPunishment:
    settings: GuildSettings
    evidence_channel: Any
    member: Any | None
    roles: tuple[tuple[Any, ManualPunishmentRoleSettings], ...]


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


class PunishmentDetailsModal(discord.ui.Modal):
    def __init__(
        self,
        controller: ManualPunishmentController,
        source_message: Any,
        selection: PunishmentSelection,
    ) -> None:
        super().__init__(title="Punishment details", timeout=300)
        self.controller = controller
        self.source_message = source_message
        self.selection = selection
        self.reason = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=MAX_REASON_LENGTH,
        )
        self.add_item(self.reason)
        self.mute_duration: discord.ui.TextInput | None = None
        if selection.mute:
            self.mute_duration = discord.ui.TextInput(
                label="Mute duration",
                placeholder="30m, 2h, 3d, or 1w",
                required=True,
                max_length=8,
            )
            self.add_item(self.mute_duration)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        duration_label = None
        duration_seconds = None
        if self.mute_duration is not None:
            duration_label = self.mute_duration.value.strip().lower()
            try:
                duration_seconds = parse_mute_duration(duration_label)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        await self.controller.execute(
            interaction,
            self.source_message,
            self.selection,
            reason=self.reason.value.strip(),
            mute_duration_label=duration_label,
            mute_duration_seconds=duration_seconds,
        )


class PunishmentActionView(discord.ui.View):
    def __init__(
        self,
        controller: ManualPunishmentController,
        source_message: Any,
        *,
        moderator_id: int,
        roles: tuple[Any, ...],
    ) -> None:
        super().__init__(timeout=300)
        self.controller = controller
        self.source_message = source_message
        self.moderator_id = moderator_id
        self.selection = PunishmentSelection()
        self.roles = roles

        self.evidence_button = discord.ui.Button(
            label="Add evidence: On",
            style=discord.ButtonStyle.success,
            row=0,
        )
        self.evidence_button.callback = self._toggle_evidence
        self.add_item(self.evidence_button)

        self.member_action = discord.ui.Select(
            placeholder="Member action: None",
            options=self._member_action_options(),
            row=1,
        )
        self.member_action.callback = self._select_member_action
        self.add_item(self.member_action)

        self.role_select: discord.ui.Select | None = None
        if roles:
            self.role_select = discord.ui.Select(
                placeholder="Role n’t: None",
                options=[
                    discord.SelectOption(
                        label=role.name,
                        value=str(role.id),
                        default=False,
                    )
                    for role in roles
                ],
                min_values=0,
                max_values=len(roles),
                row=2,
            )
            self.role_select.callback = self._select_roles
            self.add_item(self.role_select)

        self.confirm_button = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.danger,
            row=3,
        )
        self.confirm_button.callback = self._confirm
        self.add_item(self.confirm_button)

        self.cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=3,
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
                label=label,
                value=action.value,
                default=selected is action,
            )
            for label, action in (
                ("None", MemberAction.NONE),
                ("Mute", MemberAction.MUTE),
                ("Kick", MemberAction.KICK),
                ("Ban", MemberAction.BAN),
            )
        ]

    def _refresh(self) -> None:
        self.evidence_button.label = (
            "Add evidence: On"
            if self.selection.capture_evidence
            else "Add evidence: Off"
        )
        self.evidence_button.style = (
            discord.ButtonStyle.success
            if self.selection.capture_evidence
            else discord.ButtonStyle.secondary
        )
        self.member_action.placeholder = (
            f"Member action: {self.selection.member_action.value.title()}"
        )
        self.member_action.options = self._member_action_options()
        if self.role_select is not None:
            selected = set(self.selection.role_ids)
            for option in self.role_select.options:
                option.default = int(option.value) in selected
            self.role_select.placeholder = (
                f"Role n’t: {len(selected)} selected" if selected else "Role n’t: None"
            )

    async def _toggle_evidence(self, interaction: discord.Interaction) -> None:
        self.selection.toggle_evidence()
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _select_member_action(self, interaction: discord.Interaction) -> None:
        self.selection.select_member_action(self.member_action.values[0])
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _select_roles(self, interaction: discord.Interaction) -> None:
        assert self.role_select is not None
        self.selection.select_roles(
            tuple(int(role_id) for role_id in self.role_select.values)
        )
        self._refresh()
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.controller.confirm(interaction, self)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Manual punishment cancelled.",
            embed=None,
            view=None,
        )


class ManualPunishmentController:
    def __init__(self, cog: Any) -> None:
        self.cog = cog
        self.context_menu = discord.app_commands.ContextMenu(
            name="Punish",
            callback=self.open,
        )
        self.context_menu.default_permissions = discord.Permissions(
            manage_messages=True
        )
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
        existing = self.cog.bot.tree.get_command(
            self.context_menu.name,
            type=discord.AppCommandType.message,
        )
        if existing is self.context_menu:
            self.cog.bot.tree.remove_command(
                self.context_menu.name,
                type=discord.AppCommandType.message,
            )
        self._registered = False

    async def shutdown(self) -> None:
        self.unregister()

    async def confirm(
        self,
        interaction: discord.Interaction,
        view: PunishmentActionView,
    ) -> None:
        selection = PunishmentSelection(
            capture_evidence=view.selection.capture_evidence,
            member_action=view.selection.member_action,
            role_ids=view.selection.role_ids,
        )
        if not selection.capture_evidence and not selection.has_punishment:
            await interaction.response.send_message(
                "Select a punishment or enable Add evidence.",
                ephemeral=True,
            )
            return
        view.stop()
        if selection.has_punishment:
            await interaction.response.send_modal(
                PunishmentDetailsModal(self, view.source_message, selection)
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.execute(
            interaction,
            view.source_message,
            selection,
            reason=None,
            mute_duration_label=None,
            mute_duration_seconds=None,
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
        settings = GuildSettings.from_mapping(
            await self.cog.config.guild(guild).all()
        )
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
                "Messages in the manual evidence channel cannot be punished here.",
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

        roles = self._applicable_roles(guild, source_message.channel, settings)
        preview = source_message.content or "[No text content]"
        if len(preview) > PREVIEW_LENGTH:
            preview = f"{preview[: PREVIEW_LENGTH - 3]}..."
        view = PunishmentActionView(
            self,
            source_message,
            moderator_id=interaction.user.id,
            roles=tuple(role for role, _entry in roles),
        )
        await interaction.response.send_message(
            (
                "Punish this message?\n\n"
                f"Author: {source_message.author.display_name} "
                f"({source_message.author.id})\n"
                f"Channel: {source_message.channel.mention}\n\n"
                f"{preview}"
            ),
            ephemeral=True,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _applicable_roles(
        guild: Any,
        channel: Any,
        settings: GuildSettings,
    ) -> tuple[tuple[Any, ManualPunishmentRoleSettings], ...]:
        channel_id = channel_scope_id(channel)
        applicable = []
        for entry in settings.manual_punishment_roles.values():
            if channel_id not in entry.source_channel_ids:
                continue
            role = guild.get_role(entry.role_id)
            if role is not None:
                applicable.append((role, entry))
        applicable.sort(
            key=lambda item: (
                -getattr(item[0], "position", 0),
                item[0].id,
            )
        )
        return tuple(applicable[:MAX_MANUAL_PUNISHMENT_ROLES_PER_CHANNEL])

    async def _prepare(  # noqa: PLR0911 - each failure stops before irreversible effects
        self,
        interaction: Any,
        source_message: Any,
        selection: PunishmentSelection,
    ) -> PreparedPunishment | None:
        guild = source_message.guild
        settings = GuildSettings.from_mapping(
            await self.cog.config.guild(guild).all()
        )
        evidence_channel = (
            guild.get_channel(settings.manual_evidence_channel)
            if settings.manual_evidence_channel is not None
            else None
        )
        if evidence_channel is None:
            await interaction.followup.send(
                "The manual evidence channel is not configured. The source message "
                "was not deleted.",
                ephemeral=True,
            )
            return None
        if evidence_channel.id == source_message.channel.id:
            await interaction.followup.send(
                "Messages in the manual evidence channel cannot be punished here. "
                "The source message was not deleted.",
                ephemeral=True,
            )
            return None
        if not self.cog._channel_is_private(guild, evidence_channel):
            await interaction.followup.send(
                "The manual evidence channel must be private. The source message "
                "was not deleted.",
                ephemeral=True,
            )
            return None
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
            await interaction.followup.send(missing, ephemeral=True)
            return None

        member = None
        if selection.has_punishment:
            member = await _resolve_source_member(guild, source_message.author)
            if member is None:
                await interaction.followup.send(
                    "The source author is no longer available as a server member. "
                    "The source message was not deleted.",
                    ephemeral=True,
                )
                return None
            protected_check = getattr(self.cog, "_is_protected_member", None)
            if callable(protected_check) and await protected_check(member, guild):
                await interaction.followup.send(
                    "The source author is protected from moderation actions. "
                    "The source message was not deleted.",
                    ephemeral=True,
                )
                return None

        applicable_roles = self._applicable_roles(
            guild, source_message.channel, settings
        )
        applicable = {role.id: (role, entry) for role, entry in applicable_roles}
        selected_ids = set(selection.role_ids)
        for role_id in selection.role_ids:
            selected = applicable.get(role_id)
            if selected is None:
                await interaction.followup.send(
                    "A selected Role n’t is no longer available for this channel. "
                    "The source message was not deleted.",
                    ephemeral=True,
                )
                return None
            role_error = _role_nt_role_error(self.cog, guild, selected[0])
            if role_error is not None:
                await interaction.followup.send(
                    f"{role_error} The source message was not deleted.",
                    ephemeral=True,
                )
                return None
        selected_roles = tuple(
            (role, entry)
            for role, entry in applicable_roles
            if role.id in selected_ids
        )
        return PreparedPunishment(
            settings=settings,
            evidence_channel=evidence_channel,
            member=member,
            roles=selected_roles,
        )

    async def execute(
        self,
        interaction: Any,
        source_message: Any,
        selection: PunishmentSelection,
        *,
        reason: str | None,
        mute_duration_label: str | None,
        mute_duration_seconds: int | None,
    ) -> None:
        permissions = getattr(interaction, "permissions", None)
        if permissions is None or not permissions.manage_messages:
            await interaction.followup.send(
                "You need Manage Messages permission. The source message was not "
                "deleted.",
                ephemeral=True,
            )
            return
        prepared = await self._prepare(interaction, source_message, selection)
        if prepared is None:
            return
        role_names = tuple(role.name for role, _entry in prepared.roles)
        try:
            audit = await publication.create_private_audit(
                prepared.evidence_channel,
                source_message,
                interaction.user,
                selection,
                reason=reason,
                mute_duration_label=mute_duration_label,
                role_names=role_names,
            )
        except (discord.HTTPException, OSError):
            await interaction.followup.send(
                "The private audit could not be created. The source message was "
                "not deleted and no punishment was applied.",
                ephemeral=True,
            )
            return

        try:
            await source_message.delete()
        except discord.HTTPException:
            await self._finalize_audit(
                source_message,
                interaction.user,
                selection,
                audit,
                reason=reason,
                mute_duration_label=mute_duration_label,
                role_names=role_names,
                source_deletion="Failed",
                outcomes=(),
                notification_result=None,
            )
            await interaction.followup.send(
                "The private audit was created, but the source message could not "
                f"be deleted. No punishment was applied.\n{audit.primary.jump_url}",
                ephemeral=True,
            )
            return

        outcomes = await self._execute_punishments(
            source_message,
            interaction.user,
            selection,
            prepared,
            reason=reason,
            mute_duration_label=mute_duration_label,
            mute_duration_seconds=mute_duration_seconds,
        )
        await self._finalize_audit(
            source_message,
            interaction.user,
            selection,
            audit,
            reason=reason,
            mute_duration_label=mute_duration_label,
            role_names=role_names,
            source_deletion="Completed",
            outcomes=outcomes,
            notification_result=None,
        )

        notification_result = None
        if reason is not None and any(outcome.succeeded for outcome in outcomes):
            notification_result = await publication.publish_public_result(
                source_message.guild,
                source_message.channel,
                prepared.member or source_message.author,
                interaction.user,
                outcomes,
                reason=reason,
            )
            if notification_result is not None and notification_result.status != "sent":
                await self._record_failure(
                    source_message.guild.id,
                    "manual_punishment_publication",
                    f"Public notification {notification_result.status}",
                )
            await self._finalize_audit(
                source_message,
                interaction.user,
                selection,
                audit,
                reason=reason,
                mute_duration_label=mute_duration_label,
                role_names=role_names,
                source_deletion="Completed",
                outcomes=outcomes,
                notification_result=notification_result,
            )

        result_lines = [outcome.detail for outcome in outcomes]
        summary = "Manual punishment completed."
        if not outcomes:
            summary = "Evidence saved and the source message was deleted."
        if result_lines:
            summary += "\n" + "\n".join(result_lines)
        await interaction.followup.send(
            f"{summary}\n{audit.primary.jump_url}",
            ephemeral=True,
        )

    async def _execute_punishments(
        self,
        source_message: Any,
        moderator: Any,
        selection: PunishmentSelection,
        prepared: PreparedPunishment,
        *,
        reason: str | None,
        mute_duration_label: str | None,
        mute_duration_seconds: int | None,
    ) -> tuple[publication.PunishmentOutcome, ...]:
        if not selection.has_punishment:
            return ()
        assert reason is not None
        if selection.member_action in (MemberAction.KICK, MemberAction.BAN):
            effect = await self.cog._execute_action(
                source_message.guild,
                prepared.member or source_message.author,
                source_message.created_at,
                prepared.settings,
                reason=reason,
                origin=ModerationOrigin.MANUAL,
                action=selection.member_action.value,
                moderator=moderator,
            )
            kind = selection.member_action.value
            if effect.status is EffectStatus.SUCCEEDED:
                if getattr(effect, "modlog_failed", False):
                    await self._record_failure(
                        source_message.guild.id,
                        "manual_punishment_modlog",
                        f"ModLog failed after manual {kind}",
                    )
                return (
                    publication.PunishmentOutcome.succeeded_action(
                        kind,
                        effect.label or f"{kind.title()}: Applied",
                    ),
                )
            if effect.status is EffectStatus.PLANNED:
                return (
                    publication.PunishmentOutcome.planned(
                        kind,
                        effect.label or f"{kind.title()}: Planned by dry run",
                    ),
                )
            return (
                publication.PunishmentOutcome.failed(
                    kind,
                    effect.label or f"{kind.title()}: Failed",
                ),
            )

        if not await self.cog._punitive_effect_allowed(source_message.guild):
            planned = [
                publication.PunishmentOutcome.planned(
                    "role",
                    f"Role n’t {role.name}: Planned by dry run",
                    role_id=role.id,
                    role_name=role.name,
                    notification_channel_id=entry.notification_channel_id,
                )
                for role, entry in prepared.roles
            ]
            if selection.mute:
                planned.append(
                    publication.PunishmentOutcome.planned(
                        "mute",
                        f"Mute: Planned for {mute_duration_label}",
                        duration_label=mute_duration_label,
                    )
                )
            return tuple(planned)

        outcomes = list(
            await self._apply_roles(
                prepared.member,
                prepared.roles,
                moderator,
                reason,
            )
        )
        if selection.mute:
            assert mute_duration_label is not None
            assert mute_duration_seconds is not None
            outcomes.append(
                await self._apply_mute(
                    source_message,
                    member=prepared.member,
                    moderator=moderator,
                    duration_label=mute_duration_label,
                    duration_seconds=mute_duration_seconds,
                    reason=reason,
                )
            )
        return tuple(outcomes)

    async def _apply_roles(
        self,
        member: Any,
        roles: tuple[tuple[Any, ManualPunishmentRoleSettings], ...],
        moderator: Any,
        reason: str,
    ) -> tuple[publication.PunishmentOutcome, ...]:
        existing_ids = {role.id for role in getattr(member, "roles", ())}
        outcomes = []
        pending = []
        pending_entries = []
        for role, entry in roles:
            if role.id in existing_ids:
                outcomes.append(
                    publication.PunishmentOutcome.already_applied(
                        role.id,
                        role.name,
                        notification_channel_id=entry.notification_channel_id,
                    )
                )
            else:
                pending.append(role)
                pending_entries.append(entry)
        if not pending:
            return tuple(outcomes)
        try:
            await member.add_roles(
                *pending,
                reason=f"Manual punishment by {moderator} ({moderator.id}): {reason}",
            )
        except discord.HTTPException:
            outcomes.extend(
                publication.PunishmentOutcome.failed(
                    "role", f"Role n’t {role.name}: Failed"
                )
                for role in pending
            )
        else:
            outcomes.extend(
                publication.PunishmentOutcome.role_succeeded(
                    role.id,
                    role.name,
                    notification_channel_id=entry.notification_channel_id,
                )
                for role, entry in zip(pending, pending_entries, strict=True)
            )
        return tuple(outcomes)

    async def _apply_mute(
        self,
        source_message: Any,
        *,
        member: Any,
        moderator: Any,
        duration_label: str,
        duration_seconds: int,
        reason: str,
    ) -> publication.PunishmentOutcome:
        mutes = self.cog.bot.get_cog("Mutes")
        if mutes is None:
            return publication.PunishmentOutcome.failed(
                "mute", "Mute: Failed because the Mutes cog is unavailable"
            )
        mute_config = getattr(mutes, "config", None)
        if mute_config is None:
            return publication.PunishmentOutcome.failed(
                "mute", "Mute: Failed because its role is not configured"
            )
        role_id = await mute_config.guild(source_message.guild).mute_role()
        if not role_id or source_message.guild.get_role(role_id) is None:
            return publication.PunishmentOutcome.failed(
                "mute", "Mute: Failed because its role is not configured"
            )
        until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
        try:
            response = await mutes.mute_user(
                source_message.guild,
                moderator,
                member,
                until=until,
                reason=reason,
            )
        except discord.HTTPException:
            return publication.PunishmentOutcome.failed("mute", "Mute: Failed")
        if not getattr(response, "success", False):
            return publication.PunishmentOutcome.failed(
                "mute", "Mute: Rejected by the Mutes cog"
            )
        try:
            await modlog.create_case(
                self.cog.bot,
                source_message.guild,
                source_message.created_at,
                "smute",
                member,
                moderator,
                reason,
                until=until,
                channel=None,
            )
        except Exception:
            await self._record_failure(
                source_message.guild.id,
                "manual_punishment_modlog",
                "ModLog failed after manual mute",
            )
        return publication.PunishmentOutcome.mute_succeeded(duration_label)

    async def _finalize_audit(
        self,
        source_message: Any,
        moderator: Any,
        selection: PunishmentSelection,
        audit: publication.PrivateAudit,
        *,
        reason: str | None,
        mute_duration_label: str | None,
        role_names: tuple[str, ...],
        source_deletion: str,
        outcomes: tuple[publication.PunishmentOutcome, ...],
        notification_result: publication.PublicNotificationResult | None,
    ) -> None:
        try:
            await publication.finalize_private_audit(
                audit,
                source_message,
                moderator,
                selection,
                reason=reason,
                mute_duration_label=mute_duration_label,
                role_names=role_names,
                source_deletion=source_deletion,
                outcomes=outcomes,
                notification_result=notification_result,
            )
        except discord.HTTPException:
            await self._record_failure(
                source_message.guild.id,
                "manual_punishment_audit",
                "Private audit could not be finalized",
            )

    async def _record_failure(self, guild_id: int, source: str, summary: str) -> None:
        record = getattr(self.cog, "_record_operational_failure", None)
        if callable(record):
            await record(guild_id, source, summary)


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


def _role_nt_payload(
    configured: dict[int, ManualPunishmentRoleSettings],
) -> dict[str, dict[str, object]]:
    return {
        str(role_id): {
            "source_channel_ids": list(entry.source_channel_ids),
            "notification_channel_id": entry.notification_channel_id,
        }
        for role_id, entry in configured.items()
    }


async def _configured_role_nt(
    cog: Any, guild: Any
) -> tuple[Any, dict[int, ManualPunishmentRoleSettings]]:
    setting = cog.config.guild(guild).manual_punishment_roles
    raw = await setting()
    configured = GuildSettings.from_mapping(
        {"manual_punishment_roles": raw}
    ).manual_punishment_roles
    return setting, configured


async def _send_pagified(ctx: Any, content: str) -> None:
    for page in pagify(content, page_length=2_000, delims=["\n", ", "]):
        await ctx.send(
            page,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _role_nt_role_error(cog: Any, guild: Any, role: Any) -> str | None:
    is_default = getattr(role, "is_default", None)
    if callable(is_default) and is_default():
        return "The default server role cannot be a Role n’t."
    if getattr(role, "managed", False):
        return "Managed roles cannot be used as Role n’t punishments."
    return cog._missing_role_assignment_permission(guild, role)


def _role_nt_channel_limit_exceeded(
    configured: dict[int, ManualPunishmentRoleSettings],
    role_id: int,
    channel_ids: set[int],
) -> bool:
    return any(
        sum(
            channel_id in entry.source_channel_ids
            for configured_role_id, entry in configured.items()
            if configured_role_id != role_id
        )
        >= MAX_MANUAL_PUNISHMENT_ROLES_PER_CHANNEL
        for channel_id in channel_ids
    )


async def role_nt_add(cog: Any, ctx: Any, role: Any, channels: list[Any]) -> None:
    channels = list(channels)
    if not channels:
        raise commands.UserFeedbackCheckFailure("Provide at least one source channel.")
    role_error = _role_nt_role_error(cog, ctx.guild, role)
    if role_error is not None:
        raise commands.UserFeedbackCheckFailure(role_error)
    setting, configured = await _configured_role_nt(cog, ctx.guild)
    channel_ids = {channel.id for channel in channels}
    if _role_nt_channel_limit_exceeded(configured, role.id, channel_ids):
        raise commands.UserFeedbackCheckFailure(
            "A source channel cannot expose more than 25 Role n’t options."
        )
    previous = configured.get(role.id)
    source_channel_ids = list(previous.source_channel_ids if previous else ())
    for channel in channels:
        if channel.id not in source_channel_ids:
            source_channel_ids.append(channel.id)
    configured[role.id] = ManualPunishmentRoleSettings(
        role_id=role.id,
        source_channel_ids=tuple(source_channel_ids),
        notification_channel_id=(
            previous.notification_channel_id if previous is not None else None
        ),
    )
    await setting.set(_role_nt_payload(configured))
    labels = ", ".join(f"#{channel.name}" for channel in channels)
    await _send_pagified(
        ctx,
        f"Role n’t {role.name} is available in {labels}.",
    )


async def role_nt_remove_channels(
    cog: Any, ctx: Any, role: Any, channels: list[Any]
) -> None:
    channels = list(channels)
    if not channels:
        raise commands.UserFeedbackCheckFailure("Provide at least one source channel.")
    setting, configured = await _configured_role_nt(cog, ctx.guild)
    current = configured.get(role.id)
    if current is None:
        raise commands.UserFeedbackCheckFailure("That Role n’t is not configured.")
    remove_ids = {channel.id for channel in channels}
    remaining = tuple(
        channel_id
        for channel_id in current.source_channel_ids
        if channel_id not in remove_ids
    )
    if not remaining:
        raise commands.UserFeedbackCheckFailure(
            "A Role n’t needs at least one source channel. Use the remove command "
            "to delete the complete configuration."
        )
    configured[role.id] = ManualPunishmentRoleSettings(
        role_id=role.id,
        source_channel_ids=remaining,
        notification_channel_id=current.notification_channel_id,
    )
    await setting.set(_role_nt_payload(configured))
    labels = ", ".join(f"#{channel.name}" for channel in channels)
    await _send_pagified(
        ctx,
        f"Removed {labels} from Role n’t {role.name}.",
    )


async def role_nt_notification(
    cog: Any, ctx: Any, role: Any, channel: Any | None = None
) -> None:
    setting, configured = await _configured_role_nt(cog, ctx.guild)
    current = configured.get(role.id)
    if current is None:
        raise commands.UserFeedbackCheckFailure("That Role n’t is not configured.")
    if channel is None:
        configured_channel = (
            ctx.guild.get_channel(current.notification_channel_id)
            if current.notification_channel_id is not None
            else None
        )
        label = (
            f"#{configured_channel.name}"
            if configured_channel is not None
            else "the source channel"
        )
        await ctx.send(
            f"Role n’t {role.name} notifications use {label}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    permission_error = cog._missing_channel_permissions(ctx.guild, channel)
    if permission_error is not None:
        raise commands.UserFeedbackCheckFailure(permission_error)
    configured[role.id] = ManualPunishmentRoleSettings(
        role_id=role.id,
        source_channel_ids=current.source_channel_ids,
        notification_channel_id=channel.id,
    )
    await setting.set(_role_nt_payload(configured))
    await ctx.send(
        f"Role n’t {role.name} notifications will be sent to #{channel.name}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def role_nt_notification_clear(cog: Any, ctx: Any, role: Any) -> None:
    setting, configured = await _configured_role_nt(cog, ctx.guild)
    current = configured.get(role.id)
    if current is None:
        raise commands.UserFeedbackCheckFailure("That Role n’t is not configured.")
    configured[role.id] = ManualPunishmentRoleSettings(
        role_id=role.id,
        source_channel_ids=current.source_channel_ids,
        notification_channel_id=None,
    )
    await setting.set(_role_nt_payload(configured))
    await ctx.send(
        f"Role n’t {role.name} notifications will use the source channel.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def role_nt_remove(cog: Any, ctx: Any, role: Any) -> None:
    setting, configured = await _configured_role_nt(cog, ctx.guild)
    if configured.pop(role.id, None) is None:
        raise commands.UserFeedbackCheckFailure("That Role n’t is not configured.")
    await setting.set(_role_nt_payload(configured))
    await ctx.send(
        f"Removed Role n’t {role.name}.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def role_nt_list(cog: Any, ctx: Any) -> None:
    _, configured = await _configured_role_nt(cog, ctx.guild)
    if not configured:
        await ctx.send("No Role n’t punishments are configured.")
        return
    lines = []
    for role_id, entry in sorted(configured.items()):
        role = ctx.guild.get_role(role_id)
        role_label = role.name if role is not None else f"Deleted role {role_id}"
        channel_labels = []
        for channel_id in entry.source_channel_ids:
            channel = ctx.guild.get_channel(channel_id)
            channel_labels.append(
                f"#{channel.name}"
                if channel is not None
                else f"deleted channel {channel_id}"
            )
        notification = ctx.guild.get_channel(entry.notification_channel_id)
        if notification is not None:
            notification_label = f"#{notification.name}"
        elif entry.notification_channel_id is None:
            notification_label = "source channel"
        else:
            notification_label = f"deleted channel {entry.notification_channel_id}"
        lines.append(
            f"{role_label}: {', '.join(channel_labels)}; notification: "
            f"{notification_label}"
        )
    await _send_pagified(
        ctx,
        "Role n’t punishments\n" + "\n".join(lines),
    )


async def show_status(cog: Any, ctx: Any) -> None:
    if not cog._group_overview_is_private(ctx):
        await ctx.send("Run this command in a private staff channel.")
        return
    settings = GuildSettings.from_mapping(await cog.config.guild(ctx.guild).all())
    channel = ctx.guild.get_channel(settings.manual_evidence_channel)
    label = f"#{channel.name}" if channel is not None else "not set"
    await ctx.send(
        "Manual punishment settings\n"
        f"Private audit channel: {label}\n"
        f"Configured Role n’t punishments: {len(settings.manual_punishment_roles)}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def clear_deleted_channel(cog: Any, channel: Any) -> None:
    setting, configured = await _configured_role_nt(cog, channel.guild)
    changed = False
    for role_id, entry in tuple(configured.items()):
        source_channel_ids = tuple(
            source_channel_id
            for source_channel_id in entry.source_channel_ids
            if source_channel_id != channel.id
        )
        notification_channel_id = entry.notification_channel_id
        if notification_channel_id == channel.id:
            notification_channel_id = None
        if not source_channel_ids:
            del configured[role_id]
            changed = True
        elif (
            source_channel_ids != entry.source_channel_ids
            or notification_channel_id != entry.notification_channel_id
        ):
            configured[role_id] = ManualPunishmentRoleSettings(
                role_id=role_id,
                source_channel_ids=source_channel_ids,
                notification_channel_id=notification_channel_id,
            )
            changed = True
    if changed:
        await setting.set(_role_nt_payload(configured))


async def clear_deleted_role(cog: Any, role: Any) -> None:
    setting, configured = await _configured_role_nt(cog, role.guild)
    if configured.pop(role.id, None) is not None:
        await setting.set(_role_nt_payload(configured))
