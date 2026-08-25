from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote_plus

import discord
from redbot.core import commands
from redbot.core.commands import Parameter
from redbot.core.utils.chat_formatting import humanize_list

from .arguments import (
    MAX_ARGUMENT_INDEX,
    PLACEHOLDER_PATTERN,
    normalized_converter_name,
    numeric_placeholders,
)
from .catalog import CustomCommand, CustomCommandCatalog, CustomResponse

MIN_PREFIX_MESSAGE_LENGTH = 2


class RuntimeArgumentError(ValueError):
    pass


class InvalidStoredResponse(ValueError):
    pass


class CustomCommandOnCooldown(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Custom command is on cooldown for {retry_after:.1f} seconds")


class CustomCommandRuntime:
    """Resolve, parse, render, throttle, and send custom command responses."""

    def __init__(
        self,
        bot: Any,
        catalog: CustomCommandCatalog,
        operational_errors: Any,
        *,
        random_index: Callable[[int], int] = random.randrange,
        logger: Any,
    ):
        self._bot = bot
        self._catalog = catalog
        self._operational_errors = operational_errors
        self._random_index = random_index
        self._logger = logger
        self._cooldown_deadlines: dict[tuple[str, int, str, int], float] = {}

    @staticmethod
    def select_response(command: CustomCommand, boundary: int) -> CustomResponse:
        total = sum(response.weight for response in command.responses)
        if not 0 <= boundary < total:
            raise ValueError("Weighted response boundary is outside the total weight")
        upper_bound = 0
        for response in command.responses:
            upper_bound += response.weight
            if boundary < upper_bound:
                return response
        raise RuntimeError("Weighted response selection did not resolve a response")

    def choose_response(self, command: CustomCommand) -> CustomResponse:
        total = sum(response.weight for response in command.responses)
        if total <= 0:
            raise InvalidStoredResponse("Custom command has no positive response weight")
        return self.select_response(command, self._random_index(total))

    @staticmethod
    def prepare_args(raw_response: str) -> Mapping[str, Any]:
        placeholders = numeric_placeholders(raw_response)
        if not placeholders:
            return {}

        converter_types = {
            "bool": bool,
            "complex": complex,
            "float": float,
            "frozenset": frozenset,
            "int": int,
            "list": list,
            "set": set,
            "str": str,
            "tuple": tuple,
            "query": quote_plus,
        }
        first_index = min(item.index for item in placeholders)
        positions = {item.index - first_index for item in placeholders}
        final_position = max(positions)
        if final_position > MAX_ARGUMENT_INDEX:
            raise RuntimeArgumentError("Too many arguments")
        missing = set(range(final_position + 1)) - positions
        if missing:
            rendered = ", ".join(
                str(position + first_index) for position in sorted(missing)
            )
            raise RuntimeArgumentError(
                f"Arguments must be sequential. Missing arguments: {rendered}"
            )

        annotations: list[Any] = [Parameter.empty] * (final_position + 1)
        for placeholder in placeholders:
            converter_name = normalized_converter_name(placeholder.converter)
            if converter_name is None:
                continue
            annotation = converter_types.get(converter_name)
            if annotation is None:
                annotation = next(
                    (
                        value
                        for name, value in vars(discord).items()
                        if name.casefold() == converter_name
                        and isinstance(value, type)
                    ),
                    Parameter.empty,
                )
            position = placeholder.index - first_index
            current = annotations[position]
            if (
                annotation is not Parameter.empty
                and current is not Parameter.empty
                and annotation != current
            ):
                raise RuntimeArgumentError(
                    f"Conflicting converters for argument {placeholder.index}"
                )
            if annotation is not Parameter.empty:
                annotations[position] = annotation

        parameters = {}
        for position, annotation in enumerate(annotations):
            prefix = "text" if annotation is Parameter.empty else annotation.__name__.lower()
            suffix: int | str = position if position < final_position else "final"
            name = f"{prefix}_{suffix}"
            kind = (
                Parameter.KEYWORD_ONLY
                if position == final_position
                else Parameter.POSITIONAL_OR_KEYWORD
            )
            parameters[name] = Parameter(name, kind, annotation=annotation)
        return parameters

    @classmethod
    def render_response(
        cls,
        message: Any,
        raw_response: str,
        arguments: Sequence[Any],
    ) -> str:
        numeric = numeric_placeholders(raw_response)
        first_index = min((item.index for item in numeric), default=0)
        numeric_by_token = {item.token: item for item in numeric}
        context = {
            "message": message,
            "author": message.author,
            "channel": message.channel,
            "guild": message.guild,
            "server": message.guild,
        }

        def replace(match: re.Match[str]) -> str:
            token = match.group(1)
            placeholder = numeric_by_token.get(token)
            if placeholder is not None:
                value = arguments[placeholder.index - first_index]
                return cls._render_value(token, placeholder.attribute, value)
            return cls._render_context(token, context)

        return PLACEHOLDER_PATTERN.sub(replace, raw_response)

    @staticmethod
    def _stringify_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            rendered_items = tuple(str(item) for item in value)
        except TypeError:
            return str(value)
        return humanize_list(rendered_items)

    @classmethod
    def _render_value(cls, token: str, attribute: str | None, value: Any) -> str:
        if attribute is None:
            return cls._stringify_value(value)
        name = attribute.removeprefix(".")
        original = "{" + token + "}"
        if not name or name.startswith("_") or "." in name:
            return original
        return cls._stringify_value(getattr(value, name, original))

    @staticmethod
    def _render_context(token: str, context: Mapping[str, Any]) -> str:
        if token in context:
            return str(context[token])
        owner, separator, attribute = token.partition(".")
        original = "{" + token + "}"
        if (
            not separator
            or owner not in context
            or not attribute
            or attribute.startswith("_")
            or "." in attribute
        ):
            return original
        return str(getattr(context[owner], attribute, original))

    @staticmethod
    def _cooldown_key(command: CustomCommand, ctx: Any, scope: str) -> tuple[str, int, str, int]:
        subject_id = {
            "guild": ctx.guild.id,
            "channel": ctx.channel.id,
            "member": ctx.author.id,
        }[scope]
        return command.name, command.guild_id, scope, subject_id

    def check_cooldowns(self, command: CustomCommand, ctx: Any) -> None:
        now = time.monotonic()
        keys = tuple(
            (self._cooldown_key(command, ctx, scope), seconds)
            for scope, seconds in command.cooldowns.items()
        )
        remaining = tuple(
            deadline - now
            for key, _seconds in keys
            if (deadline := self._cooldown_deadlines.get(key, 0.0)) > now
        )
        if remaining:
            raise CustomCommandOnCooldown(max(remaining))
        for key, seconds in keys:
            self._cooldown_deadlines[key] = now + seconds

    async def handle_message(self, message: Any) -> None:  # noqa: PLR0911
        if (
            message.guild is None
            or message.author.bot
            or len(message.content) < MIN_PREFIX_MESSAGE_LENGTH
            or isinstance(message.channel, discord.PartialMessageable)
        ):
            return
        ctx = await self._bot.get_context(message)
        if ctx.prefix is None or not ctx.invoked_with:
            return
        if ctx.invoked_with != ctx.invoked_with.casefold():
            return
        try:
            command = await self._catalog.get(message.guild.id, ctx.invoked_with)
        except Exception as error:
            await self._report(ctx, "read stored custom command", error)
            return
        if command is None:
            return
        try:
            response = self.choose_response(command)
            self.check_cooldowns(command, ctx)
            parameters = self.prepare_args(response.content)
        except CustomCommandOnCooldown as error:
            await self._send_cooldown_feedback(ctx, error.retry_after)
            return
        except Exception as error:
            await self._report(ctx, "prepare stored custom command", error)
            return

        fake_command = commands.command(name=ctx.invoked_with)(self._callback)
        fake_command.params = parameters
        fake_command.requires.ready_event.set()
        ctx.command = fake_command
        await self._bot.invoke(ctx)
        if ctx.command_failed:
            return
        arguments = (*ctx.args[1:], *ctx.kwargs.values())
        try:
            rendered = self.render_response(ctx.message, response.content, arguments)
        except Exception as error:
            await self._report(ctx, "render stored custom command", error)
            return
        if not rendered:
            await self._report(
                ctx,
                "send custom command response",
                InvalidStoredResponse("Custom command rendered an empty response"),
            )
            return
        try:
            await ctx.send(rendered)
        except Exception as error:
            await self._report(ctx, "send custom command response", error)

    async def _send_cooldown_feedback(self, ctx: Any, retry_after: float) -> None:
        seconds = max(1, int(retry_after + 0.999))
        unit = "second" if seconds == 1 else "seconds"
        try:
            await ctx.send(f"Try again in {seconds} {unit}")
        except Exception as error:
            await self._report(ctx, "send custom command cooldown", error)

    async def _report(self, ctx: Any, action: str, error: BaseException) -> None:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            self._logger.error(
                "CustomCommands error occurred without a guild: %s",
                action,
                exc_info=error,
            )
            return
        channel = getattr(ctx, "channel", None)
        thread_id = (
            getattr(channel, "id", None)
            if getattr(channel, "parent", None) is not None
            else None
        )
        try:
            await self._operational_errors.report(
                guild_id=guild.id,
                source="CustomCommands",
                action=action,
                error=error,
                channel_id=getattr(channel, "id", None),
                thread_id=thread_id,
                message_id=getattr(getattr(ctx, "message", None), "id", None),
            )
        except Exception:
            self._logger.exception("Failed to report CustomCommands operational error")

    @staticmethod
    async def _callback(*_args: Any, **_kwargs: Any) -> None:
        return None
