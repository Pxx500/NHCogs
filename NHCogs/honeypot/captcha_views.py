"""Discord CAPTCHA presentation, with all decisions owned by JoinWatch."""

from __future__ import annotations

import asyncio
import io
import secrets

import discord

VERIFY_CUSTOM_ID = "honeypot:captcha:verify"
PANEL_TEXT = "Complete the quick check below to lift your account restriction."


def _deadline_text(result) -> str:
    deadline = result.deadline
    if deadline is None:
        return ""
    return (
        f" Your existing ban deadline still applies: <t:{int(deadline.timestamp())}:F>."
        " Contacting a moderator does not pause it"
    )


async def show_result(cog, interaction, result) -> None:
    """Update the same private reply without exposing operational metadata."""
    view = None
    attachments = []
    if result.status == "question":
        content = f"Quick check: {result.stage + 1} of 2\n{result.question.prompt}"
        attachments = [discord.File(io.BytesIO(result.question.image_png), filename="quick-check.png")]
        view = CaptchaQuestionView(cog, interaction.guild.id, interaction.user.id, result)
    elif result.status == "incorrect":
        content = "That answer wasn't correct. You can try one more full check."
        view = CaptchaRetryView(cog, interaction.guild.id, interaction.user.id)
    elif result.status == "locked":
        content = "You've used both attempts. Please DM a moderator for help." + _deadline_text(result)
    elif result.status == "preparing":
        content = "Your check is preparing. Please try again shortly."
        view = CaptchaRetryView(cog, interaction.guild.id, interaction.user.id, label="Check again")
    elif result.status == "complete":
        content = "Your check is complete and your restriction has been lifted."
    elif result.status == "complete_restricted":
        content = "You passed the check, but your restriction still requires moderator review. Please DM a moderator."
    elif result.status == "release_pending":
        content = "Your answers are correct, but we couldn't finish lifting the restriction yet. Please try again shortly."
        view = CaptchaRetryView(cog, interaction.guild.id, interaction.user.id, label="Check again")
    elif result.status == "dry_run":
        content = "This test is running in preview mode. Your roles haven't changed."
    elif result.status == "stale":
        content = "This check is no longer active. Use Verify to open your current check."
    elif result.status in {"error", "protected", "ambiguous"}:
        content = "We couldn't complete your check. Please DM a moderator for help." + _deadline_text(result)
    else:
        content = "A check isn't available for your restriction. Please DM a moderator for help."
    await interaction.edit_original_response(
        content=content,
        attachments=attachments,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _start(cog, interaction) -> None:
    result = await cog._joinwatch_verification.start(interaction.user)
    await show_result(cog, interaction, result)


class CaptchaView(discord.ui.View):
    """Keep unexpected failures private while reporting them to maintainers."""

    async def on_error(self, interaction, error, item) -> None:
        self.cog._support.schedule_error(source="Honeypot", action="CAPTCHA interaction", error=error)
        await interaction.edit_original_response(content="We couldn't complete your check. Please DM a moderator for help", view=None, attachments=[], allowed_mentions=discord.AllowedMentions.none())


class VerifyPanelView(CaptchaView):
    """One persistent public Verify handler shared by the panel and invitations."""

    def __init__(self, cog) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        button = discord.ui.Button(label="Verify", style=discord.ButtonStyle.primary, custom_id=VERIFY_CUSTOM_ID)
        button.callback = self.verify
        self.add_item(button)

    async def verify(self, interaction) -> None:
        # thinking=True creates a private reply rather than updating the public panel.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.edit_original_response(content="Use Verify in the server.")
            return
        await _start(self.cog, interaction)


class CaptchaRetryView(CaptchaView):
    def __init__(self, cog, guild_id: int, user_id: int, *, label="Try again") -> None:
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.user_id = cog, guild_id, user_id
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=secrets.token_urlsafe(24))
        button.callback = self.retry
        self.add_item(button)

    async def retry(self, interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None or interaction.guild.id != self.guild_id or interaction.user.id != self.user_id:
            return
        await _start(self.cog, interaction)


class CaptchaQuestionView(CaptchaView):
    """Six opaque answer buttons bound to one participant and consumed stage."""

    def __init__(self, cog, guild_id: int, user_id: int, result) -> None:
        super().__init__(timeout=300)
        self.cog, self.guild_id, self.user_id = cog, guild_id, user_id
        self.session_id, self.stage = result.session_id, result.stage
        self._lock = asyncio.Lock()
        self._consumed = False
        for choice in range(6):
            button = discord.ui.Button(label=str(choice + 1), style=discord.ButtonStyle.secondary, row=choice // 3, custom_id=secrets.token_urlsafe(24))

            async def answer(interaction, selected=choice):
                await self.answer(interaction, selected)

            button.callback = answer
            self.add_item(button)

    async def answer(self, interaction, choice: int) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None or interaction.guild.id != self.guild_id or interaction.user.id != self.user_id:
            return
        async with self._lock:
            if self._consumed:
                return
            result = await self.cog._joinwatch_verification.submit(
                interaction.user, self.session_id, self.stage, choice
            )
            self._consumed = True
            await show_result(self.cog, interaction, result)
