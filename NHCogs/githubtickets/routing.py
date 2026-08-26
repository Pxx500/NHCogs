from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from .models import PresenceTier

_PRESENCE_PRIORITY = {
    PresenceTier.ONLINE: 0,
    PresenceTier.IDLE: 1,
    PresenceTier.DO_NOT_DISTURB: 2,
    PresenceTier.OFFLINE: 3,
}


@dataclass(frozen=True, slots=True)
class CandidateFacts:
    user_id: int
    is_cached_member: bool
    has_participant_role: bool
    can_manage_messages: bool
    matches_profile: bool
    was_pinged: bool
    timed_out: bool
    declined: bool
    unassigned: bool
    presence_tier: PresenceTier
    active_assignment_count: int
    last_ping_at: datetime | None


def select_reviewer(
    candidates: Iterable[CandidateFacts],
    *,
    chooser: Callable[[Sequence[CandidateFacts]], CandidateFacts] = random.choice,
) -> CandidateFacts | None:
    eligible = tuple(candidate for candidate in candidates if _is_eligible(candidate))
    if not eligible:
        return None
    best_priority = min(_priority_key(candidate) for candidate in eligible)
    tied = tuple(candidate for candidate in eligible if _priority_key(candidate) == best_priority)
    return tied[0] if len(tied) == 1 else chooser(tied)


def _is_eligible(candidate: CandidateFacts) -> bool:
    return (
        candidate.is_cached_member
        and (candidate.has_participant_role or candidate.can_manage_messages)
        and candidate.matches_profile
        and not candidate.was_pinged
        and not candidate.timed_out
        and not candidate.declined
        and not candidate.unassigned
    )


def _priority_key(candidate: CandidateFacts) -> tuple[object, ...]:
    return (
        _PRESENCE_PRIORITY[candidate.presence_tier],
        candidate.active_assignment_count,
        candidate.last_ping_at is not None,
        candidate.last_ping_at,
    )
