"""Available account metadata captured at an observation, without fetching users."""

from datetime import datetime, timezone

import discord


def _timestamp(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    return None


def account_snapshot(user) -> dict | None:
    """Keep missing member-only fields unknown when Discord supplies a User."""
    created_at = _timestamp(getattr(user, "created_at", None))
    created_at_source = "discord_profile" if created_at is not None else None
    user_id = getattr(user, "id", user)
    if (
        created_at is None
        and isinstance(user_id, int)
        and not isinstance(user_id, bool)
        and 0 < user_id < (1 << 64)
    ):
        created_at = _timestamp(discord.utils.snowflake_time(user_id))
        created_at_source = "discord_snowflake"
    snapshot = {
        "username": getattr(user, "name", None),
        "global_name": getattr(user, "global_name", None),
        "nickname": getattr(user, "nick", None),
        "account_created_at": created_at,
        "account_created_at_source": created_at_source,
        "guild_joined_at": _timestamp(getattr(user, "joined_at", None)),
        "bot": getattr(user, "bot", None),
        "system": getattr(user, "system", None),
        "public_flags": getattr(getattr(user, "public_flags", None), "value", None),
    }
    return snapshot if any(value is not None for value in snapshot.values()) else None
