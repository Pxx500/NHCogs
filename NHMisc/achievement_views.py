from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .achievement_definitions import AchievementDefinition
    from .achievement_store import AchievementDeletionPreview
    from .nhmisc import NHMisc


class AchievementGrantView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        source_message: discord.Message,
        opener_id: int,
        candidates: tuple[Any, ...],
        definitions: tuple[AchievementDefinition, ...],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.source_message = source_message
        self.opener_id = opener_id
        self.candidates = candidates
        self.definitions = definitions
        self.selected_user_ids = {member.id for member in candidates}
        self.selected_keys = {definition.key for definition in definitions}
        self.message: discord.Message | None = None
        self._configure_selects()

    @property
    def candidate_ids(self) -> tuple[int, ...]:
        return tuple(member.id for member in self.candidates)

    def replace_candidates(self, candidates: tuple[Any, ...]) -> None:
        previous_ids = set(self.candidate_ids)
        previous_selected = set(self.selected_user_ids)
        self.candidates = candidates
        self.selected_user_ids = {
            member.id
            for member in candidates
            if member.id in previous_selected or member.id not in previous_ids
        }
        self._configure_selects()

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        user_lines = [
            f"<@{member.id}>"
            for member in self.candidates
            if member.id in self.selected_user_ids
        ] or ["None"]
        achievement_lines = [
            definition.display_name
            for definition in self.definitions
            if definition.key in self.selected_keys
        ] or ["None"]
        lines = [
            f"Source: [Open message]({self.source_message.jump_url})",
            "",
            "Recipients:",
            *user_lines,
            "",
            "Achievements:",
            *achievement_lines,
        ]
        if notice:
            lines.extend(("", notice))
        return discord.Embed(
            title="Grant achievements",
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
        self._disable_controls()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(
        placeholder="Choose recipients",
        min_values=0,
        max_values=1,
        row=0,
    )
    async def recipient_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        self.selected_user_ids = {int(value) for value in select.values}
        self._configure_confirm()
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.select(
        placeholder="Choose achievements",
        min_values=0,
        max_values=1,
        row=1,
    )
    async def achievement_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        self.selected_keys = set(select.values)
        self._configure_confirm()
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Grant achievements",
        style=discord.ButtonStyle.green,
        row=2,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._confirm_achievement_grant(interaction, self)

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
        await interaction.response.edit_message(
            content="Achievement grant cancelled",
            embed=None,
            view=None,
        )

    def _configure_selects(self) -> None:
        self.recipient_select.options = [
            discord.SelectOption(
                label=member.display_name[:100],
                value=str(member.id),
                default=member.id in self.selected_user_ids,
            )
            for member in self.candidates
        ]
        self.recipient_select.max_values = len(self.candidates)
        self.achievement_select.options = [
            discord.SelectOption(
                label=definition.display_name[:100],
                value=definition.key,
                default=definition.key in self.selected_keys,
            )
            for definition in self.definitions
        ]
        self.achievement_select.max_values = len(self.definitions)
        self._configure_confirm()

    def _configure_confirm(self) -> None:
        self.confirm.disabled = not self.selected_user_ids or not self.selected_keys

    def _disable_controls(self) -> None:
        for child in self.children:
            child.disabled = True


class AchievementRevokeView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        opener_id: int,
        members: tuple[Any, ...],
        definitions: tuple[AchievementDefinition, ...],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = opener_id
        self.members = members
        self.definitions = definitions
        self.selected_keys: set[str] = set()
        self.message: discord.Message | None = None
        self.achievement_select.options = [
            discord.SelectOption(
                label=definition.display_name[:100],
                value=definition.key,
            )
            for definition in definitions
        ]
        self.achievement_select.max_values = len(definitions)
        self.confirm.disabled = True

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        selected = [
            definition
            for definition in self.definitions
            if definition.key in self.selected_keys
        ]
        role_lines = [
            f"{definition.display_name}: <@&{definition.role_id}>"
            for definition in selected
            if definition.role_id is not None
        ]
        lines = [
            "Recipients:",
            *(f"<@{member.id}>" for member in self.members),
            "",
            "Achievements to revoke:",
            *(definition.display_name for definition in selected),
        ]
        if role_lines:
            lines.extend(("", "Roles to remove:", *role_lines))
        if notice:
            lines.extend(("", notice))
        return discord.Embed(
            title="Revoke achievements",
            description="\n".join(lines),
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
        try:
            await self.message.delete()
        except discord.HTTPException:
            pass

    @discord.ui.select(
        placeholder="Choose achievements to revoke",
        min_values=0,
        max_values=1,
        row=0,
    )
    async def achievement_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        self.selected_keys = set(select.values)
        self.confirm.disabled = not self.selected_keys
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Revoke achievements",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._confirm_achievement_revoke(interaction, self)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.defer()
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


class GateRevokeView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        opener_id: int,
        member: discord.Member,
        award: Any,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = opener_id
        self.member = member
        self.award = award
        self.message: discord.Message | None = None

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        return self.cog._build_gate_revoke_embed(
            self.award.guild_id,
            self.member,
            self.award,
            notice=notice,
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
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Revoke Gate",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._confirm_gate_revoke(interaction, self)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Gate revoke cancelled",
            embed=None,
            view=None,
        )


class AchievementRoleBindView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        opener_id: int,
        role: discord.Role,
        holder_ids: tuple[int, ...],
        definitions: tuple[AchievementDefinition, ...],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = opener_id
        self.role = role
        self.holder_ids = holder_ids
        self.definitions = definitions
        self.selected_key: str | None = None
        self.message: discord.Message | None = None
        self.achievement_select.options = [
            discord.SelectOption(
                label=definition.display_name[:100],
                value=definition.key,
            )
            for definition in definitions
        ]
        self.confirm.disabled = True

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        selected = next(
            (
                definition.display_name
                for definition in self.definitions
                if definition.key == self.selected_key
            ),
            "Select an achievement",
        )
        embed = discord.Embed(title="Bind achievement role")
        embed.add_field(name="Achievement", value=selected, inline=False)
        embed.add_field(name="Discord role", value=self.role.mention, inline=False)
        embed.add_field(
            name="Current role holders",
            value=str(len(self.holder_ids)),
            inline=False,
        )
        embed.add_field(
            name="Achievements to import without proof",
            value=str(len(self.holder_ids)),
            inline=False,
        )
        if notice:
            embed.description = notice
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opener_id:
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this can use it",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.achievement_select.disabled = True
        self.confirm.disabled = True
        self.cancel.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
    @discord.ui.select(placeholder="Select achievement", min_values=1, max_values=1)
    async def achievement_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        self.selected_key = select.values[0]
        self.confirm.disabled = False
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, row=1)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._confirm_achievement_role_bind(interaction, self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.defer()
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


class AchievementRoleReplaceView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        opener_id: int,
        definition: AchievementDefinition,
        old_role: discord.Role,
        new_role: discord.Role,
        *,
        stored_holder_ids: tuple[int, ...],
        old_holder_ids: tuple[int, ...],
        new_holder_ids: tuple[int, ...],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = opener_id
        self.definition = definition
        self.old_role = old_role
        self.new_role = new_role
        self.stored_holder_ids = stored_holder_ids
        self.old_holder_ids = old_holder_ids
        self.new_holder_ids = new_holder_ids
        self.message: discord.Message | None = None

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        imported_count = len(
            (set(self.old_holder_ids) | set(self.new_holder_ids))
            - set(self.stored_holder_ids)
        )
        embed = discord.Embed(
            title="Replace achievement role",
            description=(
                "Choose whether members keep the old role. Selecting an option "
                "applies the replacement immediately"
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="Achievement",
            value=f"{self.definition.display_name} (`{self.definition.key}`)",
            inline=False,
        )
        embed.add_field(name="Old role", value=self.old_role.mention, inline=True)
        embed.add_field(name="New role", value=self.new_role.mention, inline=True)
        embed.add_field(
            name="Stored achievement holders",
            value=str(len(self.stored_holder_ids)),
            inline=True,
        )
        embed.add_field(
            name="Current old-role holders",
            value=str(len(self.old_holder_ids)),
            inline=True,
        )
        embed.add_field(
            name="Current new-role holders",
            value=str(len(self.new_holder_ids)),
            inline=True,
        )
        embed.add_field(
            name="Achievements to import without proof",
            value=str(imported_count),
            inline=True,
        )
        if notice:
            embed.add_field(name="Cannot replace", value=notice, inline=False)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opener_id:
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this review can control it",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.replacement_mode.disabled = True
        self.cancel.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.select(
        placeholder="Choose replacement behavior",
        min_values=1,
        max_values=1,
        options=(
            discord.SelectOption(
                label="Move members to the new role",
                value="move",
                description="Remove the old role from its current holders",
            ),
            discord.SelectOption(
                label="Keep the old role on members",
                value="keep",
                description="Leave the old role assigned but stop tracking it",
            ),
        ),
    )
    async def replacement_mode(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        await self.cog._confirm_achievement_role_replace(
            interaction,
            self,
            remove_old=select.values[0] == "move",
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Achievement role replacement cancelled",
            embed=None,
            view=None,
        )


class AchievementDeleteView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        opener_id: int,
        preview: AchievementDeletionPreview,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = opener_id
        self.preview = preview
        self.message: discord.Message | None = None

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="Delete achievement",
            description=(
                "This permanently deletes the achievement and every stored award for it"
            ),
            color=discord.Color.red(),
        )
        embed.add_field(
            name="Achievement",
            value=self.preview.definition.display_name,
            inline=False,
        )
        embed.add_field(
            name="Key",
            value=f"`{self.preview.definition.key}`",
            inline=False,
        )
        embed.add_field(
            name="Stored awards",
            value=str(self.preview.award_count),
            inline=False,
        )
        if notice:
            embed.add_field(name="Cannot delete", value=notice, inline=False)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opener_id:
            return True
        await interaction.response.send_message(
            "Only the moderator who opened this review can control it",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self.confirm.disabled = True
        self.cancel.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Delete achievement",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._confirm_achievement_delete(interaction, self)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Achievement deletion cancelled",
            embed=None,
            view=None,
        )


class _GateProofSelect(discord.ui.Select):
    def __init__(
        self,
        proof_view: GateProofView,
        candidate: Any,
        *,
        row: int,
    ) -> None:
        self.proof_view = proof_view
        self.user_id = candidate.member.id
        selected = proof_view.assignments[self.user_id]
        display_name = candidate.member.display_name
        if candidate.missing_ordinals:
            options = [
                discord.SelectOption(
                    label=f"{display_name}: Don't add proof"[:100],
                    value="none",
                    default=selected is None,
                ),
                *(
                    discord.SelectOption(
                        label=f"{display_name}: Gate {ordinal}"[:100],
                        value=str(ordinal),
                        default=selected == ordinal,
                    )
                    for ordinal in candidate.missing_ordinals
                ),
            ]
            disabled = False
        else:
            options = [
                discord.SelectOption(
                    label=f"{display_name}: No Gate proofs missing"[:100],
                    value="none",
                    default=True,
                )
            ]
            disabled = True
        super().__init__(
            placeholder=display_name[:100],
            min_values=1,
            max_values=1,
            options=options,
            disabled=disabled,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        self.proof_view.assignments[self.user_id] = (
            None if value == "none" else int(value)
        )
        self.proof_view._configure_page()
        await interaction.response.edit_message(
            embed=self.proof_view.render_embed(),
            view=self.proof_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class GateProofView(discord.ui.View):
    PAGE_SIZE = 4

    def __init__(
        self,
        cog: NHMisc,
        source_message: discord.Message,
        opener_id: int,
        candidates: tuple[Any, ...],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.source_message = source_message
        self.opener_id = opener_id
        self.candidates = candidates
        self.assignments: dict[int, int | None] = {
            candidate.member.id: None for candidate in candidates
        }
        self.page_index = 0
        self.reviewing = False
        self.message: discord.Message | None = None
        self._user_selects: list[_GateProofSelect] = []
        self._configure_page()

    @property
    def candidate_ids(self) -> tuple[int, ...]:
        return tuple(candidate.member.id for candidate in self.candidates)

    @property
    def selected_assignments(self) -> dict[int, int]:
        return {
            user_id: ordinal
            for user_id, ordinal in self.assignments.items()
            if ordinal is not None
        }

    @property
    def page_count(self) -> int:
        return max(1, (len(self.candidates) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        if self.reviewing:
            assignments = self.selected_assignments
            selection_lines = [
                f"<@{user_id}>: Gate {ordinal}"
                for user_id, ordinal in assignments.items()
            ] or ["No proofs selected"]
            description = [
                f"Source: [Open message]({self.source_message.jump_url})",
                "",
                "Proofs to attach:",
                *selection_lines,
            ]
        else:
            start = self.page_index * self.PAGE_SIZE
            page_candidates = self.candidates[start : start + self.PAGE_SIZE]
            selection_lines = []
            for candidate in page_candidates:
                ordinal = self.assignments[candidate.member.id]
                if not candidate.missing_ordinals:
                    selection = "No Gate proofs missing"
                elif ordinal is None:
                    selection = "Don't add proof"
                else:
                    selection = f"Gate {ordinal}"
                selection_lines.append(f"<@{candidate.member.id}>: {selection}")
            description = [
                f"Source: [Open message]({self.source_message.jump_url})",
                f"Page {self.page_index + 1}/{self.page_count}",
                "",
                *selection_lines,
            ]
        if notice:
            description.extend(("", notice))
        return discord.Embed(
            title="Add Gate Proof",
            description="\n".join(description),
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
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        row=4,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.reviewing:
            self.reviewing = False
        else:
            self.page_index -= 1
        self._configure_page()
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.secondary,
        row=4,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.page_index += 1
        self._configure_page()
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Review",
        style=discord.ButtonStyle.primary,
        row=4,
    )
    async def review(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.reviewing:
            await self.cog._confirm_gate_proofs(interaction, self)
            return
        self.reviewing = True
        self._configure_page()
        await interaction.response.edit_message(
            embed=self.render_embed(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        row=4,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Gate proof attachment cancelled",
            embed=None,
            view=None,
        )

    def _configure_page(self) -> None:
        for select in self._user_selects:
            self.remove_item(select)
        self._user_selects.clear()
        if not self.reviewing:
            start = self.page_index * self.PAGE_SIZE
            for row, candidate in enumerate(
                self.candidates[start : start + self.PAGE_SIZE]
            ):
                select = _GateProofSelect(self, candidate, row=row)
                self._user_selects.append(select)
                self.add_item(select)
        self._configure_controls()

    def _configure_controls(self) -> None:
        self.previous.label = "Back" if self.reviewing else "Previous"
        self.previous.disabled = not self.reviewing and self.page_index == 0
        self.next.disabled = self.reviewing or self.page_index >= self.page_count - 1
        self.review.label = "Attach proofs" if self.reviewing else "Review"
        self.review.style = (
            discord.ButtonStyle.green
            if self.reviewing
            else discord.ButtonStyle.primary
        )
        self.review.disabled = not self.selected_assignments


class GateProofBatchView(discord.ui.View):
    def __init__(
        self,
        cog: NHMisc,
        source_message: discord.Message,
        opener_id: int,
        member: discord.Member,
        entries: tuple[Any, ...],
        *,
        existing_proofs: dict[int, Any],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.source_message = source_message
        self.opener_id = opener_id
        self.member = member
        self.entries = entries
        self.existing_proofs = existing_proofs
        self.message: discord.Message | None = None
        if existing_proofs:
            self.attach.label = "Replace and add all"
            self.add_missing.disabled = all(
                entry.ordinal in existing_proofs for entry in entries
            )
        else:
            self.remove_item(self.add_missing)

    def render_embed(self, *, notice: str | None = None) -> discord.Embed:
        proof_lines = [
            f"Gate {entry.ordinal}: [Open proof]({entry.jump_url})"
            for entry in self.entries
            if entry.ordinal not in self.existing_proofs
        ]
        replacement_lines = []
        if self.existing_proofs:
            guild_id = self.source_message.guild.id
            for entry in self.entries:
                existing = self.existing_proofs.get(entry.ordinal)
                if existing is None:
                    continue
                current_url = (
                    "https://discord.com/channels/"
                    f"{guild_id}/{existing.source_channel_id}/"
                    f"{existing.source_message_id}"
                )
                replacement_lines.append(
                    f"Gate {entry.ordinal}: [Current proof]({current_url}) → "
                    f"[New proof]({entry.jump_url})"
                )
        description = [
            "This only attaches proofs to existing Gates. It does not add or "
            "increment any Gate",
            "",
            f"Player: <@{self.member.id}>",
            f"Request: [Open message]({self.source_message.jump_url})",
        ]
        if replacement_lines:
            description.extend(
                (
                    "",
                    "Choose whether to replace existing proofs and add every "
                    "missing proof, or add only missing proofs",
                )
            )
        if replacement_lines:
            description.extend(("", "Proofs to replace:", *replacement_lines))
        if proof_lines:
            description.extend(("", "Proofs to attach:", *proof_lines))
        if notice:
            description.extend(("", notice))
        return discord.Embed(
            title="Batch Gate proof import detected",
            description="\n".join(description),
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
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Attach proofs",
        style=discord.ButtonStyle.green,
    )
    async def attach(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._confirm_gate_proof_batch(
            interaction,
            self,
            replace_existing=bool(self.existing_proofs),
        )

    @discord.ui.button(
        label="Add missing only",
        style=discord.ButtonStyle.primary,
    )
    async def add_missing(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await self.cog._confirm_gate_proof_batch(
            interaction,
            self,
            replace_existing=False,
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Gate proof batch cancelled",
            embed=None,
            view=None,
        )
