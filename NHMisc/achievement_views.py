from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .achievement_definitions import AchievementDefinition
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
