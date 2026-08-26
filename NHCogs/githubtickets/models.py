from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class RoutingMode(str, Enum):
    NONE = "none"
    AUTOMATIC = "automatic"
    DIRECT_WAIT = "direct_wait"
    DIRECT_AUTOMATIC = "direct_automatic"


class TicketState(str, Enum):
    CREATING = "creating"
    OPEN = "open"
    CLAIMED = "claimed"
    FINISHING = "finishing"


class NextAction(str, Enum):
    DIRECT_PING = "direct_ping"
    AUTOMATIC_PING = "automatic_ping"
    TARGET_TIMEOUT = "target_timeout"


class PresenceTier(str, Enum):
    ONLINE = "online"
    IDLE = "idle"
    DO_NOT_DISTURB = "do_not_disturb"
    OFFLINE = "offline"


class ExclusionReason(str, Enum):
    DECLINED = "declined"
    UNASSIGNED = "unassigned"
    TIMED_OUT = "timed_out"


class InvalidCategoryName(ValueError):
    pass


class CategoryAlreadyExists(ValueError):
    pass


class CategoryLimitReached(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Category:
    category_id: int
    guild_id: int
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Profile:
    guild_id: int
    user_id: int
    github_username: str | None
    automatic_pings: bool
    category_ids: tuple[int, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CandidateHistory:
    user_id: int
    active_assignment_count: int
    last_ping_at: datetime | None
    was_pinged: bool
    declined: bool
    unassigned: bool
    timed_out: bool


@dataclass(frozen=True, slots=True)
class NewTicket:
    guild_id: int
    channel_id: int
    author_id: int
    pr_title: str
    pr_url: str
    category_display: str
    routing_mode: RoutingMode
    direct_target_id: int | None
    category_ids: tuple[int, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket_id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    thread_id: int | None
    author_id: int
    pr_title: str
    pr_url: str
    category_display: str
    routing_mode: RoutingMode
    state: TicketState
    direct_target_id: int | None
    current_target_id: int | None
    assignee_id: int | None
    ping_count: int
    protection_until: datetime | None
    next_action: NextAction | None
    next_action_at: datetime | None
    pending_target_id: int | None
    pending_presence_tier: PresenceTier | None
    pending_ping_automatic: bool | None
    pending_response_deadline: datetime | None
    created_at: datetime
    updated_at: datetime
    transition_version: int
    category_ids: tuple[int, ...]
    public_token: str = ""
    pending_ping_reserved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TicketExclusion:
    ticket_id: int
    user_id: int
    reason: ExclusionReason
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TicketPing:
    ticket_id: int
    sequence_number: int
    target_user_id: int
    presence_tier: PresenceTier | None
    automatic: bool
    sent_at: datetime
    response_deadline: datetime


@dataclass(frozen=True, slots=True)
class PingReservation:
    ticket_id: int
    target_user_id: int
    presence_tier: PresenceTier | None
    automatic: bool
    reserved_at: datetime
    response_deadline: datetime
