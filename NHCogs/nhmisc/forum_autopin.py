from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord

log = logging.getLogger("red.NHMisc")

RETRY_SECONDS = 1.0
AUDIT_REASON = "NHMisc forum starter-message autopin"

# Returns True when the alert was actually delivered, so the service only
# suppresses repeats it knows a moderator has seen.
AlertSender = Callable[["discord.Guild", str], Awaitable[bool]]


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

    def missing_permissions(
        self, guild: discord.Guild, channel: discord.ForumChannel
    ) -> str | None:
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel:
            return f"I need permission to view {channel.mention}."
        if not permissions.read_message_history:
            return f"I need Read Message History permission in {channel.mention}."
        if not permissions.pin_messages:
            return f"I need Pin Messages permission in {channel.mention}."
        return None

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
