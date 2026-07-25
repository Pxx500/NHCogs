from typing import TYPE_CHECKING

import discord

from redbot.core.i18n import Translator

from .case_review import (
    CaseFeedbackItem,
    available_image_review_actions,
    case_custom_id,
)
from .detection_cases import AttachmentKey

if TYPE_CHECKING:
    from .honeypot import Honeypot


_ = Translator("Honeypot", __file__)


# Interaction response policy: use ephemeral messages only for useful information
# that is not already visible in the public message, such as permission denials,
# errors, conflicts, or confirmation prompts. Do not repeat successful actions
# when the updated embed, content, or disabled controls already show the result.
class DetectionCaseView(discord.ui.View):
    """Persistent controls whose callbacks always resolve state through SQLite."""

    def __init__(
        self,
        cog: "Honeypot",
        case_id: str,
        *,
        has_image_feedback: bool,
        feedback_items: tuple[CaseFeedbackItem, ...] = (),
        message_sequence: int | None = None,
        resolved: bool = False,
        allow_individual: bool = True,
        moderation_actions: tuple[str, ...] = ("ban", "kick", "ignore"),
    ) -> None:
        super().__init__()
        self.timeout = None
        self.cog = cog
        self.case_id = case_id
        self.message_sequence = message_sequence
        scope = f"message-{message_sequence}" if message_sequence is not None else None
        add_item = getattr(self, "add_item", None)
        if not callable(add_item):
            return
        if self.message_sequence is None and not resolved:
            for label, action, style, emoji in (
                ("Ban", "ban", discord.ButtonStyle.danger, "🔨"),
                ("Kick", "kick", discord.ButtonStyle.secondary, "👢"),
                ("Ignore", "ignore", discord.ButtonStyle.success, "✅"),
            ):
                if action not in moderation_actions:
                    continue
                button = discord.ui.Button(
                    label=label,
                    style=style,
                    emoji=emoji,
                    custom_id=case_custom_id(case_id, "moderate", action),
                    disabled=resolved,
                    row=0,
                )

                async def moderation_callback(interaction, selected=action):
                    await self.cog._case_review_moderation_interaction(
                        interaction, self.case_id, selected
                    )

                button.callback = moderation_callback
                add_item(button)
        if resolved or not has_image_feedback:
            return
        available_actions = available_image_review_actions(feedback_items)
        labels = {
            "tp": "All TP" if "fp" in available_actions else "Add all",
            "fp": "All FP",
            "ignore": "Ignore",
        }
        styles = {
            "tp": discord.ButtonStyle.success,
            "fp": discord.ButtonStyle.danger,
            "ignore": discord.ButtonStyle.secondary,
        }
        for action in available_actions:
            label = labels[action]
            style = styles[action]
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=case_custom_id(
                    case_id,
                    f"{scope}-resolve" if scope is not None else "resolve",
                    action,
                ),
                disabled=resolved,
                row=1,
            )

            async def callback(interaction, selected=action):
                if self.message_sequence is None:
                    await self.cog._case_review_bulk_interaction(
                        interaction, self.case_id, selected
                    )
                else:
                    await self.cog._case_review_message_bulk_interaction(
                        interaction,
                        self.case_id,
                        self.message_sequence,
                        selected,
                    )

            button.callback = callback
            add_item(button)
        if not allow_individual:
            return
        individual = discord.ui.Button(
            label="Individual",
            style=discord.ButtonStyle.primary,
            custom_id=case_custom_id(
                case_id,
                f"{scope}-images" if scope is not None else "images",
                "individual",
            ),
            disabled=resolved,
            row=1,
        )

        async def individual_callback(interaction):
            if self.message_sequence is None:
                await self.cog._case_review_individual_prompt(
                    interaction, self.case_id
                )
            else:
                await self.cog._case_review_individual_prompt(
                    interaction,
                    self.case_id,
                    message_sequence=self.message_sequence,
                )

        individual.callback = individual_callback
        add_item(individual)


class DetectionBulkConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: "Honeypot",
        case_id: str,
        action: str,
        *,
        message_sequence: int | None = None,
        confirm_label: str | None = None,
        expected_keys: tuple[AttachmentKey, ...] = (),
    ) -> None:
        super().__init__()
        self.timeout = 60
        self.cog = cog
        self.case_id = case_id
        self.action = action
        self.message_sequence = message_sequence
        self.expected_keys = expected_keys
        add_item = getattr(self, "add_item", None)
        if not callable(add_item):
            return
        label = confirm_label or (
            "Confirm All TP" if action == "tp" else "Confirm All FP"
        )
        style = (
            discord.ButtonStyle.success
            if action == "tp"
            else discord.ButtonStyle.danger
        )
        button = discord.ui.Button(label=label, style=style)

        async def callback(interaction):
            await self.cog._dismiss_case_review_prompt(interaction)
            if self.message_sequence is None:
                await self.cog._case_review_bulk_interaction(
                    interaction,
                    self.case_id,
                    self.action,
                    confirmed=True,
                    expected_keys=self.expected_keys,
                )
            else:
                await self.cog._case_review_message_bulk_interaction(
                    interaction,
                    self.case_id,
                    self.message_sequence,
                    self.action,
                    confirmed=True,
                    expected_keys=self.expected_keys,
                )

        button.callback = callback
        add_item(button)


class DetectionModerationConfirmationView(discord.ui.View):
    def __init__(self, cog: "Honeypot", case_id: str, action: str) -> None:
        super().__init__()
        self.timeout = 60
        self.cog = cog
        self.case_id = case_id
        self.action = action
        add_item = getattr(self, "add_item", None)
        if not callable(add_item):
            return
        button = discord.ui.Button(
            label=f"Confirm {action.title()}",
            style=(
                discord.ButtonStyle.danger
                if action == "ban"
                else discord.ButtonStyle.secondary
            ),
        )

        async def callback(interaction):
            await self.cog._dismiss_case_review_prompt(interaction)
            await self.cog._case_review_moderation_interaction(
                interaction, self.case_id, self.action, confirmed=True
            )

        button.callback = callback
        add_item(button)


class DetectionIndividualView(discord.ui.View):
    def __init__(
        self, cog: "Honeypot", feedback_items: tuple[CaseFeedbackItem, ...]
    ) -> None:
        super().__init__()
        self.timeout = 300
        self.cog = cog
        self.selected_key: AttachmentKey | None = None
        add_item = getattr(self, "add_item", None)
        if not callable(add_item):
            return
        choices = feedback_items[:25]
        items = {str(index): item for index, item in enumerate(choices)}
        selector = discord.ui.Select(
            placeholder="Choose an image",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=(
                        f"{item.key.message_sequence}.{item.key.position + 1} "
                        f"{item.filename}"
                    )[:100],
                    value=str(index),
                )
                for index, item in enumerate(choices)
            ],
            row=0,
        )
        action_buttons = []

        def replace_action_buttons(item: CaseFeedbackItem) -> None:
            remove_item = getattr(self, "remove_item", None)
            for button in action_buttons:
                if callable(remove_item):
                    remove_item(button)
                elif hasattr(self, "children") and button in self.children:
                    self.children.remove(button)
            action_buttons.clear()
            available_actions = available_image_review_actions((item,))
            labels = {
                "tp": "TP" if "fp" in available_actions else "Add",
                "fp": "FP",
                "ignore": "Ignore",
            }
            styles = {
                "tp": discord.ButtonStyle.success,
                "fp": discord.ButtonStyle.danger,
                "ignore": discord.ButtonStyle.secondary,
            }
            for action in available_actions:
                label = labels[action]
                style = styles[action]
                button = discord.ui.Button(
                    label=label,
                    style=style,
                    disabled=False,
                    row=1,
                )

                async def callback(interaction, selected=action):
                    if self.selected_key is None:
                        return
                    await self.cog._dismiss_case_review_prompt(interaction)
                    await self.cog._case_review_attachment_interaction(
                        interaction, self.selected_key, selected
                    )

                button.callback = callback
                action_buttons.append(button)
                add_item(button)

        async def select_callback(interaction):
            selected_value = selector.values[0]
            selected_item = items[selected_value]
            self.selected_key = selected_item.key
            for option in selector.options:
                option.default = option.value == selected_value
            replace_action_buttons(selected_item)
            await interaction.response.edit_message(
                content=_("Choose the result for the selected image."), view=self
            )

        selector.callback = select_callback
        add_item(selector)
