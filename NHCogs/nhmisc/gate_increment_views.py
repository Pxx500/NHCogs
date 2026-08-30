from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .nhmisc import NHMisc

class GateIncrementReviewView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        source_message: discord.Message,
        opener_id: int,
        candidates: tuple[Any, ...],
        *,
        custom_achievements: tuple[Any, ...] = (),
        ephemeral: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.source_message = source_message
        self.opener_id = opener_id
        self.ephemeral = ephemeral
        self.message: discord.Message | None = None
        self.candidates = candidates
        self.custom_achievements = custom_achievements
        self.selected_custom_achievement_keys: set[str] = set()
        self.selected_user_ids = {
            candidate.user_id
            for candidate in candidates
            if candidate.target_role_id is not None
        }
        self.solo_gater_enabled = False
        self._configure_select()
        self._configure_achievement_select()
        self._configure_solo_toggle()

    @property
    def candidate_ids(self) -> tuple[int, ...]:
        return tuple(candidate.user_id for candidate in self.candidates)

    def replace_candidates(self, candidates: tuple[Any, ...]) -> None:
        previous_candidate_ids = set(self.candidate_ids)
        previous_selected_ids = set(self.selected_user_ids)
        previously_selected = set(self.selected_user_ids)
        self.candidates = candidates
        self.selected_user_ids = {
            candidate.user_id
            for candidate in candidates
            if candidate.target_role_id is not None
            and (
                candidate.user_id in previous_selected_ids
                or candidate.user_id not in previous_candidate_ids
            )
        }
        if self.selected_user_ids != previously_selected:
            self.solo_gater_enabled = False
        self._configure_select()
        self._configure_solo_toggle()

    def replace_custom_achievements(self, achievements: tuple[Any, ...]) -> None:
        self.custom_achievements = achievements
        live_keys = {achievement.key for achievement in achievements}
        self.selected_custom_achievement_keys.intersection_update(live_keys)
        self._configure_achievement_select()

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        selectable_count = sum(
            candidate.target_role_id is not None for candidate in self.candidates
        )
        lines = [
            f"Source: [Open message]({self.source_message.jump_url})",
            f"Selected: {len(self.selected_user_ids)} of {selectable_count} users",
            "",
        ]
        for candidate in self.candidates:
            if (
                candidate.target_role_id is not None
                and candidate.user_id not in self.selected_user_ids
            ):
                continue
            current = (
                f"<@&{candidate.current_gate_role_ids[-1]}>"
                if candidate.current_gate_role_ids
                else "No Gate"
            )
            if candidate.target_role_id is None:
                lines.append(
                    f"⛔ <@{candidate.user_id}> {current} maximum tier"
                )
            else:
                lines.append(
                    f"<@{candidate.user_id}> {current} → "
                    f"<@&{candidate.target_role_id}>"
                )
                if (
                    candidate.target_ordinal is not None
                    and candidate.target_ordinal != candidate.highest_ordinal + 1
                ):
                    lines.append(
                        f"⚠️ <@{candidate.user_id}> will fill missing Stargate "
                        f"{candidate.target_ordinal} instead of adding Stargate "
                        f"{candidate.highest_ordinal + 1}"
                    )
        if len(self.selected_user_ids) == 1:
            selected = next(
                candidate
                for candidate in self.candidates
                if candidate.user_id in self.selected_user_ids
            )
            if selected.has_solo_gater:
                lines.extend(("", "Solo Gater: already assigned"))
            elif self.solo_gater_enabled:
                lines.extend(("", "Solo Gater: will be assigned"))
        if self.selected_custom_achievement_keys:
            lines.extend(
                (
                    "",
                    "Achievements: "
                    f"{len(self.selected_custom_achievement_keys)} selected",
                )
            )
        if notice:
            lines.extend(("", notice))
        return discord.Embed(
            title="Gate increment review",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opener_id:
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this review can control it",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        if not self.ephemeral:
            try:
                await self.message.delete()
                return
            except discord.HTTPException:
                pass
        self._disable_controls()
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.select(
        placeholder="Choose users to increment",
        min_values=0,
        max_values=1,
        row=0,
    )
    async def candidate_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        selected_user_ids = {int(value) for value in select.values}
        if selected_user_ids != self.selected_user_ids:
            self.solo_gater_enabled = False
        self.selected_user_ids = selected_user_ids
        self.confirm.disabled = not self.selected_user_ids
        self._configure_solo_toggle()
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.select(
        placeholder="Choose additional achievements",
        min_values=0,
        max_values=1,
        row=1,
    )
    async def achievement_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        self.selected_custom_achievement_keys = set(select.values)
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Confirm increment",
        style=discord.ButtonStyle.green,
        row=2,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._confirm_gate_increment_review(interaction, self)

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._refresh_gate_increment_review(interaction, self)

    @discord.ui.button(
        label="☐ Solo Gater",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def solo_gater(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.solo_gater_enabled = not self.solo_gater_enabled
        self._configure_solo_toggle()
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        row=2,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        if self.ephemeral:
            await interaction.response.edit_message(
                content="Gate increment cancelled",
                embed=None,
                view=None,
            )
            return
        await interaction.response.defer()
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass

    def _configure_select(self) -> None:
        options = []
        for candidate in self.candidates:
            if candidate.target_role_id is None:
                continue
            current = (
                f"Gate {candidate.current_tier}"
                if candidate.current_tier is not None
                else "No Gate"
            )
            target = f"Gate {candidate.current_tier + 1 if candidate.current_tier else 1}"
            options.append(
                discord.SelectOption(
                    label=candidate.display_name[:100],
                    value=str(candidate.user_id),
                    description=f"{current} → {target}"[:100],
                    default=candidate.user_id in self.selected_user_ids,
                )
            )
        if options:
            self.candidate_select.options = options
            self.candidate_select.max_values = len(options)
            self.candidate_select.disabled = False
        else:
            self.candidate_select.options = [
                discord.SelectOption(
                    label="No incrementable users",
                    value="none",
                )
            ]
            self.candidate_select.max_values = 1
            self.candidate_select.disabled = True
        self.confirm.disabled = not self.selected_user_ids

    def _configure_solo_toggle(self) -> None:
        if len(self.selected_user_ids) != 1:
            self.solo_gater_enabled = False
            if self.solo_gater in self.children:
                self.remove_item(self.solo_gater)
            return
        selected = next(
            candidate
            for candidate in self.candidates
            if candidate.user_id in self.selected_user_ids
        )
        if self.solo_gater not in self.children:
            self.add_item(self.solo_gater)
        if selected.has_solo_gater:
            self.solo_gater_enabled = False
            self.solo_gater.label = "☑ Solo Gater (already assigned)"
            self.solo_gater.disabled = True
            self.solo_gater.style = discord.ButtonStyle.secondary
            return
        self.solo_gater.label = (
            "☑ Solo Gater" if self.solo_gater_enabled else "☐ Solo Gater"
        )
        self.solo_gater.disabled = False
        self.solo_gater.style = (
            discord.ButtonStyle.primary
            if self.solo_gater_enabled
            else discord.ButtonStyle.secondary
        )

    def _configure_achievement_select(self) -> None:
        options = [
            discord.SelectOption(
                label=achievement.display_name[:100],
                value=achievement.key,
                default=achievement.key in self.selected_custom_achievement_keys,
            )
            for achievement in self.custom_achievements
        ]
        if options:
            self.achievement_select.options = options
            self.achievement_select.max_values = len(options)
            self.achievement_select.disabled = False
            return
        self.achievement_select.options = [
            discord.SelectOption(
                label="No additional achievements available",
                value="none",
            )
        ]
        self.achievement_select.max_values = 1
        self.achievement_select.disabled = True

    def _disable_controls(self) -> None:
        for child in self.children:
            child.disabled = True


class GateIncrementExistingView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        source_message: discord.Message,
        opener_id: int,
        snapshot: Any,
        *,
        ephemeral: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.source_message = source_message
        self.opener_id = opener_id
        self.snapshot = snapshot
        self.ephemeral = ephemeral
        self.message: discord.Message | None = None

    def render_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Existing Gate increment",
            description=self.cog._format_gate_increment_operation(self.snapshot),
            color=discord.Color.orange(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opener_id:
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this review can control it",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        if not self.ephemeral:
            try:
                await self.message.delete()
                return
            except discord.HTTPException:
                pass
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Resume stored operation",
        style=discord.ButtonStyle.primary,
    )
    async def resume(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._resume_gate_increment_review(interaction, self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        if self.ephemeral:
            await interaction.response.edit_message(
                content="Gate increment recovery cancelled",
                embed=None,
                view=None,
            )
            return
        await interaction.response.defer()
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass
