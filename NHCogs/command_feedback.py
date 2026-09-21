"""One user-facing reply for every failed command invocation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord
from redbot.core import commands

log = logging.getLogger("red.NHCogs")
UNEXPECTED_COMMAND_MESSAGE = "Something went wrong while running this command. The error was logged."
_IGNORED_ERROR_BASES = frozenset({BaseException, Exception, object})
_STATIC_MESSAGES = (
    ("NoPrivateMessage", "This command cannot be used in private messages"),
    ("PrivateMessageOnly", "This command can only be used in private messages"),
    ("NSFWChannelRequired", "This command can only be used in an NSFW channel"),
    ("MaxConcurrencyReached", "That command is already running"),
    ("DisabledCommand", "That command is disabled"),
)
_PERMISSION_TYPES = (
    "MissingPermissions",
    "MissingRole",
    "MissingAnyRole",
    "BotMissingRole",
    "BotMissingAnyRole",
    "NotOwner",
)
_ARGUMENT_TYPES = (
    "BadArgument",
    "BadUnionArgument",
    "BadLiteralArgument",
    "ConversionError",
    "ArgumentParsingError",
    "UnexpectedQuoteError",
    "InvalidEndOfQuotedStringError",
    "ExpectedClosingQuoteError",
    "TooManyArguments",
    "UserInputError",
)
ErrorReporter = Callable[[BaseException], Awaitable[None]]


def _error_type(name: str) -> type | None:
    kind = getattr(commands, name, None)
    if isinstance(kind, type) and kind not in _IGNORED_ERROR_BASES:
        return kind
    return None


def _matches(error: BaseException, name: str) -> bool:
    kind = _error_type(name)
    return kind is not None and isinstance(error, kind)


def _error_text(error: BaseException) -> str:
    message = getattr(error, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    args = getattr(error, "args", ())
    if args and isinstance(args[0], str) and args[0].strip():
        return args[0].strip()
    return ""


def _permission_names(error: BaseException) -> str:
    missing = getattr(error, "missing_permissions", ()) or ()
    names = []
    for permission in missing:
        raw = getattr(permission, "name", permission)
        names.append(str(raw).replace("_", " ").title())
    return ", ".join(names)


def _specific_feedback(error: BaseException) -> str | None:
    message = None
    if _matches(error, "UserFeedbackCheckFailure"):
        message = _error_text(error) or "That command could not be completed"
    elif _matches(error, "MissingRequiredArgument"):
        param = getattr(error, "param", None)
        name = getattr(param, "name", None) or "argument"
        message = f"Missing required argument `{name}`"
    elif _matches(error, "BotMissingPermissions"):
        names = _permission_names(error)
        message = (
            f"I need these permissions: {names}"
            if names
            else "I am missing permissions required for this command"
        )
    elif _matches(error, "CommandOnCooldown"):
        retry_after = getattr(error, "retry_after", None)
        message = (
            f"That command is on cooldown. Try again in {retry_after:.1f} seconds."
            if isinstance(retry_after, int | float)
            else "That command is on cooldown"
        )
    else:
        for name, static_message in _STATIC_MESSAGES:
            if _matches(error, name):
                message = static_message
                break
    return message


def _argument_message(error: BaseException) -> str | None:
    for name in _ARGUMENT_TYPES:
        if _matches(error, name):
            return _error_text(error) or "I could not understand that argument"
    return None


def _check_message(error: BaseException) -> str | None:
    for name in _PERMISSION_TYPES:
        if _matches(error, name):
            return "You do not have permission to use this command"
    if not _matches(error, "CheckFailure"):
        return None
    text = _error_text(error)
    if text and "global check" not in text.casefold():
        return text
    return "You do not have permission to use this command"


def _message_for_error(error: BaseException) -> str | None:
    return _specific_feedback(error) or _argument_message(error) or _check_message(error)


def user_facing_command_message(error: BaseException) -> str | None:
    """Return the reply for a user-facing error, or None when it is unexpected."""
    message = _message_for_error(error)
    if message is not None:
        return message
    original = getattr(error, "original", None)
    if isinstance(original, BaseException) and original is not error:
        return _message_for_error(original)
    return None


async def send_command_feedback(ctx, content: str) -> None:
    send = getattr(ctx, "send", None)
    if not callable(send):
        log.error("Could not send command error feedback")
        return
    try:
        await send(content, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("Could not send command error feedback")


async def respond_to_command_error(ctx, error: BaseException, *, report: ErrorReporter | None = None) -> None:
    """Send exactly one reply. Unexpected failures are reported, then acknowledged."""
    message = user_facing_command_message(error)
    if message is None:
        original = getattr(error, "original", None)
        reported = original if isinstance(original, BaseException) else error
        guild = getattr(ctx, "guild", None)
        if report is not None and guild is not None:
            await report(reported)
        message = UNEXPECTED_COMMAND_MESSAGE
    await send_command_feedback(ctx, message)
