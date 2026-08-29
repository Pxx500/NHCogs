from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_PROTECTION_SECONDS = 10
DEFAULT_VOLUNTEER_SECONDS = 2 * 60 * 60
DEFAULT_ONLINE_RESPONSE_SECONDS = 2 * 60 * 60
DEFAULT_IDLE_RESPONSE_SECONDS = 4 * 60 * 60
DEFAULT_DND_RESPONSE_SECONDS = 6 * 60 * 60
DEFAULT_OFFLINE_RESPONSE_SECONDS = 24 * 60 * 60
DEFAULT_DIRECT_RESPONSE_SECONDS = 24 * 60 * 60
DEFAULT_MAX_PINGS = 3
DEFAULT_GITHUB_RECOVERY_SECONDS = 15 * 60


class InvalidDuration(ValueError):
    pass


class NegativeDuration(ValueError):
    pass


DEFAULTS: dict[str, object] = {
    "ticket_channel_id": None,
    "log_channel_id": None,
    "participant_role_ids": [],
    "protection_seconds": DEFAULT_PROTECTION_SECONDS,
    "volunteer_seconds": DEFAULT_VOLUNTEER_SECONDS,
    "online_response_seconds": DEFAULT_ONLINE_RESPONSE_SECONDS,
    "idle_response_seconds": DEFAULT_IDLE_RESPONSE_SECONDS,
    "dnd_response_seconds": DEFAULT_DND_RESPONSE_SECONDS,
    "offline_response_seconds": DEFAULT_OFFLINE_RESPONSE_SECONDS,
    "direct_response_seconds": DEFAULT_DIRECT_RESPONSE_SECONDS,
    "max_pings": DEFAULT_MAX_PINGS,
}

GITHUB_INTEGRATION_DEFAULTS: dict[str, object] = {
    "guild_id": None,
    "enabled": False,
    "bind_host": None,
    "bind_port": None,
    "recovery_seconds": DEFAULT_GITHUB_RECOVERY_SECONDS,
}

_DURATION_PATTERN = re.compile(r"^(?P<sign>-?)(?P<value>\d+)(?P<unit>[smh]?)$")
_DURATION_MULTIPLIERS = {"": 1, "s": 1, "m": 60, "h": 60 * 60}
_MAX_PORT = 65535


def parse_duration(raw: str) -> int:
    match = _DURATION_PATTERN.fullmatch(raw.strip().lower())
    if match is None:
        raise InvalidDuration(raw)
    value = int(match.group("value"))
    if match.group("sign") == "-" and value:
        raise NegativeDuration(raw)
    seconds = value * _DURATION_MULTIPLIERS[match.group("unit")]
    try:
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    except OverflowError as error:
        raise InvalidDuration(raw) from error
    return seconds


def _positive_id(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, str, bytes, bytearray)):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _role_ids(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    seen: set[int] = set()
    result: list[int] = []
    for item in raw:
        role_id = _positive_id(item)
        if role_id is None or role_id in seen:
            continue
        seen.add(role_id)
        result.append(role_id)
    return tuple(result)


def _nonnegative_int(raw: object, default: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return default
    return raw


def _positive_int(raw: object, default: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return default
    return raw


def _bind_host(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _bind_port(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 1 <= raw <= _MAX_PORT else None


@dataclass(frozen=True, slots=True)
class GuildSettings:
    ticket_channel_id: int | None
    log_channel_id: int | None
    participant_role_ids: tuple[int, ...]
    protection_seconds: int
    volunteer_seconds: int
    online_response_seconds: int
    idle_response_seconds: int
    dnd_response_seconds: int
    offline_response_seconds: int
    direct_response_seconds: int
    max_pings: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | object) -> GuildSettings:
        values = raw if isinstance(raw, Mapping) else {}
        return cls(
            ticket_channel_id=_positive_id(values.get("ticket_channel_id")),
            log_channel_id=_positive_id(values.get("log_channel_id")),
            participant_role_ids=_role_ids(values.get("participant_role_ids")),
            protection_seconds=_nonnegative_int(
                values.get("protection_seconds"), DEFAULT_PROTECTION_SECONDS
            ),
            volunteer_seconds=_nonnegative_int(
                values.get("volunteer_seconds"), DEFAULT_VOLUNTEER_SECONDS
            ),
            online_response_seconds=_nonnegative_int(
                values.get("online_response_seconds"),
                DEFAULT_ONLINE_RESPONSE_SECONDS,
            ),
            idle_response_seconds=_nonnegative_int(
                values.get("idle_response_seconds"), DEFAULT_IDLE_RESPONSE_SECONDS
            ),
            dnd_response_seconds=_nonnegative_int(
                values.get("dnd_response_seconds"), DEFAULT_DND_RESPONSE_SECONDS
            ),
            offline_response_seconds=_nonnegative_int(
                values.get("offline_response_seconds"),
                DEFAULT_OFFLINE_RESPONSE_SECONDS,
            ),
            direct_response_seconds=_nonnegative_int(
                values.get("direct_response_seconds"), DEFAULT_DIRECT_RESPONSE_SECONDS
            ),
            max_pings=_nonnegative_int(values.get("max_pings"), DEFAULT_MAX_PINGS),
        )


@dataclass(frozen=True, slots=True)
class GitHubIntegrationSettings:
    guild_id: int | None
    enabled: bool
    bind_host: str | None
    bind_port: int | None
    recovery_seconds: int

    @property
    def receiver_configured(self) -> bool:
        return self.bind_host is not None and self.bind_port is not None

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | object,
    ) -> GitHubIntegrationSettings:
        values = raw if isinstance(raw, Mapping) else {}
        enabled = values.get("enabled")
        return cls(
            guild_id=_positive_id(values.get("guild_id")),
            enabled=enabled if isinstance(enabled, bool) else False,
            bind_host=_bind_host(values.get("bind_host")),
            bind_port=_bind_port(values.get("bind_port")),
            recovery_seconds=_positive_int(
                values.get("recovery_seconds"),
                DEFAULT_GITHUB_RECOVERY_SECONDS,
            ),
        )
