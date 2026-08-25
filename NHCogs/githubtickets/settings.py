from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_PROTECTION_SECONDS = 10
DEFAULT_VOLUNTEER_SECONDS = 2 * 60 * 60
DEFAULT_ONLINE_RESPONSE_SECONDS = 2 * 60 * 60
DEFAULT_IDLE_RESPONSE_SECONDS = 4 * 60 * 60
DEFAULT_DND_RESPONSE_SECONDS = 6 * 60 * 60
DEFAULT_OFFLINE_RESPONSE_SECONDS = 24 * 60 * 60
DEFAULT_DIRECT_RESPONSE_SECONDS = 24 * 60 * 60
DEFAULT_MAX_PINGS = 3

DEFAULTS: dict[str, object] = {
    "ticket_channel_id": None,
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


def _positive_id(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)  # type: ignore[arg-type]
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


@dataclass(frozen=True, slots=True)
class GuildSettings:
    ticket_channel_id: int | None
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
