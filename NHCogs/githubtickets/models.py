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


class TicketOrigin(str, Enum):
    DISCORD = "discord"
    GITHUB = "github"


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


class GitHubDeliveryState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


class GitHubOutboxOperation(str, Enum):
    ADD_ASSIGNEE = "add_assignee"
    REMOVE_ASSIGNEE = "remove_assignee"


class GitHubOutboxState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PullRequestObservationState(str, Enum):
    APPLIED = "applied"
    STALE = "stale"
    CONFLICT = "conflict"


class InvalidCategoryName(ValueError):
    pass


class CategoryAlreadyExists(ValueError):
    pass


class CategoryLimitReached(ValueError):
    pass


class ActivePullRequestTicketExists(ValueError):
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
    author_id: int | None
    pr_title: str
    pr_url: str
    category_display: str
    routing_mode: RoutingMode
    direct_target_id: int | None
    category_ids: tuple[int, ...]
    created_at: datetime
    origin: TicketOrigin = TicketOrigin.DISCORD


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket_id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    thread_id: int | None
    author_id: int | None
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
    origin: TicketOrigin = TicketOrigin.DISCORD


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    repository_id: int
    pr_number: int
    github_pr_id: int
    github_author_id: int
    repository_full_name: str
    url: str
    title: str
    github_author_login: str
    draft: bool
    open: bool
    labels: tuple[str, ...]
    github_updated_at: datetime
    assignees: tuple[str, ...] = ()
    current_ticket_id: int | None = None
    last_processed_action: str | None = None


@dataclass(frozen=True, slots=True)
class PullRequestObservation:
    state: PullRequestObservationState
    pull_request: GitHubPullRequest


@dataclass(frozen=True, slots=True)
class GitHubDelivery:
    delivery_guid: str
    github_delivery_id: int | None
    event: str
    action: str | None
    installation_id: int
    repository_id: int | None
    pr_number: int | None
    received_at: datetime
    state: GitHubDeliveryState
    attempts: int
    next_attempt_at: datetime | None
    processing_started_at: datetime | None
    completed_at: datetime | None
    error_summary: str | None
    raw_body: bytes | None


@dataclass(frozen=True, slots=True)
class GitHubOutboxItem:
    outbox_id: int
    operation: GitHubOutboxOperation
    ticket_id: int
    transition_version: int
    repository_id: int
    repository_full_name: str
    pr_number: int
    github_login: str
    actor_user_id: int | None
    state: GitHubOutboxState
    attempts: int
    next_attempt_at: datetime | None
    processing_started_at: datetime | None
    error_summary: str | None
    created_at: datetime
    updated_at: datetime


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
