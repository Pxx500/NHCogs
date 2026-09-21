from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord
from redbot.core import commands

log = logging.getLogger("red.NHMisc")
FORUM_TYPE_FORUM = 15
FORUM_TYPE_MEDIA = 16
_FORUM_TYPE_VALUES = frozenset({FORUM_TYPE_FORUM, FORUM_TYPE_MEDIA})
_FORUM_TYPE_NAMES = frozenset({"forum", "media"})
_FORUM_CLASS_NAMES = frozenset({"ForumChannel", "MediaChannel"})
_THREAD_TYPE_NAMES = frozenset({"public_thread", "private_thread", "news_thread"})
_CHANNEL_REFERENCE = re.compile(
    r"^(?:<#(?P<mention>\d+)>|(?P<snowflake>\d+)|"
    r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+)/(?P<url>\d+)(?:/\d+)?)/?$"
)

RETRY_SECONDS = 1.0
AUDIT_REASON = "NHMisc forum starter-message autopin"

# Returns True when the alert was actually delivered, so the service only
# suppresses repeats it knows a moderator has seen.
AlertSender = Callable[["discord.Guild", str], Awaitable[bool]]


@dataclass(frozen=True)
class _ChannelReference:
    channel_id: int
    guild_id: int | None = None


def is_forum_channel(channel: object) -> bool:
    """True for forum and media channels, including uncached type-only objects."""
    if channel is None:
        return False
    forum_types = tuple(
        channel_type
        for name in ("ForumChannel", "MediaChannel")
        if isinstance((channel_type := getattr(discord, name, None)), type)
    )
    if forum_types and isinstance(channel, forum_types):
        return True
    channel_type = getattr(channel, "type", None)
    type_value = getattr(channel_type, "value", channel_type)
    type_name = getattr(channel_type, "name", None)
    if type_name is None and isinstance(channel_type, str):
        type_name = channel_type
    if type_value in _FORUM_TYPE_VALUES:
        return True
    if isinstance(type_name, str) and type_name.casefold() in _FORUM_TYPE_NAMES:
        return True
    return type(channel).__name__ in _FORUM_CLASS_NAMES


def _parse_channel_reference(argument: str) -> _ChannelReference | None:
    match = _CHANNEL_REFERENCE.fullmatch(argument.strip())
    if match is None:
        return None
    raw_id = match.group("mention") or match.group("snowflake") or match.group("url")
    guild_id = match.group("guild")
    return _ChannelReference(int(raw_id), int(guild_id) if guild_id is not None else None)


def _iter_guild_channels(guild) -> list:
    channels = getattr(guild, "channels", ())
    if isinstance(channels, dict):
        return list(channels.values())
    return list(channels or ())


def _context_guild_id(ctx) -> int | None:
    guild = getattr(ctx, "guild", None)
    guild_id = getattr(guild, "id", None)
    return guild_id if isinstance(guild_id, int) else None


def _channel_in_context_guild(ctx, channel) -> bool:
    guild_id = _context_guild_id(ctx)
    if guild_id is None or channel is None:
        return channel is not None
    channel_guild = getattr(channel, "guild", None)
    channel_guild_id = getattr(channel_guild, "id", None)
    if channel_guild_id is None:
        return True
    return channel_guild_id == guild_id


def _cached_channel(ctx, channel_id: int):
    guild = getattr(ctx, "guild", None)
    getter = getattr(guild, "get_channel", None)
    channel = getter(channel_id) if callable(getter) else None
    if channel is None:
        bot = getattr(ctx, "bot", None)
        getter = getattr(bot, "get_channel", None)
        channel = getter(channel_id) if callable(getter) else None
    if channel is not None and not _channel_in_context_guild(ctx, channel):
        return None
    return channel


def _forum_by_name(ctx, argument: str):
    exact = None
    folded = None
    target = argument.casefold()
    for channel in _iter_guild_channels(getattr(ctx, "guild", None)):
        if not is_forum_channel(channel):
            continue
        name = getattr(channel, "name", None)
        if not isinstance(name, str):
            continue
        if name == argument and exact is None:
            exact = channel
        elif name.casefold() == target and folded is None:
            folded = channel
    return exact or folded


def _missing_forum_message(argument: str) -> str:
    return f"I could not find a forum channel for `{argument.strip()}`."


def _not_forum_message(channel, argument: str) -> str:
    label = getattr(channel, "mention", None) or f"`{argument.strip()}`"
    channel_type = getattr(channel, "type", None)
    type_name = getattr(channel_type, "name", None)
    if type_name is None and isinstance(channel_type, str):
        type_name = channel_type
    is_thread = type(channel).__name__ == "Thread" or (
        isinstance(type_name, str) and type_name.casefold() in _THREAD_TYPE_NAMES
    )
    if is_thread:
        return f"{label} is not a forum channel. Choose the forum channel, not a post."
    return f"{label} is not a forum channel."


async def _fetch_channel(ctx, channel_id: int, argument: str):
    bot = getattr(ctx, "bot", None)
    fetch = getattr(bot, "fetch_channel", None)
    if not callable(fetch):
        return None, None
    try:
        channel = await fetch(channel_id)
    except discord.Forbidden:
        return None, "I need the View Channel permission to use that forum."
    except discord.NotFound:
        return None, None
    except discord.HTTPException:
        return None, f"I could not look up `{argument.strip()}`."
    if not _channel_in_context_guild(ctx, channel):
        return None, None
    return channel, None


def _named_forum(ctx, argument: str):
    channel = _forum_by_name(ctx, argument.strip())
    if channel is None:
        return None, f"I could not find a forum channel named `{argument.strip()}`."
    return channel, None


async def _forum_from_reference(ctx, reference: _ChannelReference, argument: str):
    channel = _cached_channel(ctx, reference.channel_id)
    failure = None
    if channel is None:
        channel, failure = await _fetch_channel(ctx, reference.channel_id, argument)
    if failure is not None:
        return None, failure
    if channel is None or not is_forum_channel(channel):
        failure = (
            _missing_forum_message(argument)
            if channel is None
            else _not_forum_message(channel, argument)
        )
        return None, failure
    return channel, None


async def resolve_forum_channel(ctx, argument: str):
    """Resolve a forum by mention, ID, URL, or name. Returns (channel, error)."""
    reference = _parse_channel_reference(argument)
    if reference is None:
        return _named_forum(ctx, argument)
    if reference.guild_id is not None and _context_guild_id(ctx) not in (None, reference.guild_id):
        return None, _missing_forum_message(argument)
    return await _forum_from_reference(ctx, reference, argument)


class ForumChannelConverter(getattr(commands, "Converter", object)):
    """Accept a forum or media channel by mention, ID, URL, or name."""

    async def convert(self, ctx: commands.Context, argument: str):
        channel, failure = await resolve_forum_channel(ctx, argument)
        if failure is not None:
            raise commands.UserFeedbackCheckFailure(failure)
        return channel


class ForumAutopinService:
    """Pins the starter message of new posts in configured forum channels."""

    def __init__(
        self,
        config,
        *,
        alert_sender: AlertSender,
        logger: logging.Logger = log,
    ) -> None:
        self._config = config
        self._send_alert = alert_sender
        self._log = logger
        self._alerted: set[tuple[int, int]] = set()

    async def get_forum_ids(self, guild: discord.Guild) -> list[int]:
        configured = await self._config.guild(guild).forum_autopin_channel_ids()
        return sorted(set(configured))

    async def enable(self, guild: discord.Guild, forum_id: int) -> bool:
        """Configure a forum. Returns False when it was already configured."""
        configured = set(await self._config.guild(guild).forum_autopin_channel_ids())
        if forum_id in configured:
            return False

        configured.add(forum_id)
        await self._store_forum_ids(guild, configured)
        return True

    async def disable(self, guild: discord.Guild, forum_id: int) -> bool:
        """Unconfigure a forum. Returns False when it was not configured."""
        configured = set(await self._config.guild(guild).forum_autopin_channel_ids())
        if forum_id not in configured:
            return False

        configured.remove(forum_id)
        await self._store_forum_ids(guild, configured)
        self._alerted.discard((guild.id, forum_id))
        return True

    def missing_permissions(self, guild: discord.Guild, channel) -> str | None:
        me = getattr(guild, "me", None)
        mention = getattr(channel, "mention", "that forum")
        if me is None:
            return "I cannot check permissions for that forum."
        try:
            permissions = channel.permissions_for(me)
        except (AttributeError, TypeError):
            return "I cannot check permissions for that forum."
        missing = [
            label
            for attribute, label in (
                ("view_channel", "View Channel"),
                ("read_message_history", "Read Message History"),
                ("pin_messages", "Pin Messages"),
            )
            if not getattr(permissions, attribute, False)
        ]
        if not missing:
            return None
        if len(missing) == 1:
            return f"I need the {missing[0]} permission in {mention}."
        listed = ", ".join(missing[:-1]) + f", and {missing[-1]}"
        return f"I need these permissions in {mention}: {listed}."

    async def handle_thread_create(self, thread: discord.Thread) -> None:
        """Pin the starter message for a new post in a configured forum."""
        parent_id = thread.parent_id
        configured = set(
            await self._config.guild(thread.guild).forum_autopin_channel_ids()
        )
        if parent_id not in configured:
            return

        starter_message = await self._fetch_starter_message(thread)
        if starter_message is None:
            return

        try:
            await starter_message.pin(reason=AUDIT_REASON)
        except discord.Forbidden:
            self._log.warning(
                "Forum autopin cannot pin starter message %s "
                "in guild %s, forum %s due to missing Pin Messages permission",
                thread.id,
                thread.guild.id,
                parent_id,
            )
            await self._alert_missing_permission(
                thread.guild, parent_id, "pin messages (Pin Messages)"
            )
            return
        except discord.NotFound:
            self._log.warning(
                "Forum autopin starter message %s disappeared in guild %s, forum %s",
                thread.id,
                thread.guild.id,
                parent_id,
            )
            return
        except discord.HTTPException:
            self._log.exception(
                "Forum autopin failed to pin starter message %s in guild %s, forum %s",
                thread.id,
                thread.guild.id,
                parent_id,
            )
            return

        self._alerted.discard((thread.guild.id, parent_id))

    async def handle_channel_delete(self, channel: discord.abc.GuildChannel) -> bool:
        """Drop autopin configuration for a deleted forum."""
        guild = channel.guild
        if not await self.disable(guild, channel.id):
            return False

        self._log.info(
            "Forum autopin configuration removed for deleted forum %s in guild %s",
            channel.id,
            guild.id,
        )
        await self._send_alert(
            guild,
            (
                f"Forum autopin is no longer configured for deleted forum "
                f"`{channel.name}` (`{channel.id}`)."
            ),
        )
        return True

    async def _store_forum_ids(
        self, guild: discord.Guild, forum_ids: set[int]
    ) -> None:
        await self._config.guild(guild).forum_autopin_channel_ids.set(sorted(forum_ids))

    async def _fetch_starter_message(
        self, thread: discord.Thread
    ) -> discord.Message | None:
        """Fetch the starter message, retrying once while Discord catches up."""
        parent_id = thread.parent_id
        for attempt in range(2):
            try:
                return await thread.fetch_message(thread.id)
            except discord.NotFound:
                if attempt == 0:
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                self._log.warning(
                    "Forum autopin could not find starter message %s "
                    "in guild %s, forum %s",
                    thread.id,
                    thread.guild.id,
                    parent_id,
                )
                return None
            except discord.Forbidden:
                self._log.warning(
                    "Forum autopin cannot fetch starter message %s "
                    "in guild %s, forum %s due to missing permissions",
                    thread.id,
                    thread.guild.id,
                    parent_id,
                )
                await self._alert_missing_permission(
                    thread.guild,
                    parent_id,
                    "read starter messages (View Channel and Read Message History)",
                )
                return None
            except discord.HTTPException:
                self._log.exception(
                    "Forum autopin failed to fetch starter message %s "
                    "in guild %s, forum %s",
                    thread.id,
                    thread.guild.id,
                    parent_id,
                )
                return None
        return None

    async def _alert_missing_permission(
        self, guild: discord.Guild, forum_id: int, missing: str
    ) -> None:
        alert_key = (guild.id, forum_id)
        if alert_key in self._alerted:
            return

        forum = guild.get_channel(forum_id)
        forum_label = forum.mention if forum is not None else f"`{forum_id}`"
        delivered = await self._send_alert(
            guild,
            (
                f"Forum autopin cannot {missing} in {forum_label}. "
                "New posts will not be pinned until the permission is restored."
            ),
        )
        if delivered:
            self._alerted.add(alert_key)
