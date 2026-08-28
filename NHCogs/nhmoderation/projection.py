from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .models import ProjectedAction, StoredObservation

CORRELATION_WINDOW = timedelta(minutes=5)
PROJECTION_VERSION = 2
BAN_ACTIONS = {"ban", "hackban", "tempban"}
ATTRIBUTION_SOURCE_PRIORITY = {
    "nhmoderation": 4,
    "honeypot": 4,
    "red_modlog": 3,
    "discord_audit": 2,
}


def _order_key(item: StoredObservation) -> tuple[datetime, int]:
    return item.occurred_at or item.observed_at, item.observation_id


def _transition_kind(item: StoredObservation) -> str | None:
    if item.action_hint in BAN_ACTIONS:
        return "ban"
    if item.action_hint == "unban":
        return "unban"
    return None


def _time_distance(left: StoredObservation, right: StoredObservation) -> float | None:
    left_time = left.occurred_at or left.observed_at
    right_time = right.occurred_at or right.observed_at
    return abs((left_time - right_time).total_seconds())


def _matches(left: StoredObservation, right: StoredObservation) -> bool:
    if left.target_user_id is None or left.target_user_id != right.target_user_id:
        return False
    distance = _time_distance(left, right)
    return distance is not None and distance <= CORRELATION_WINDOW.total_seconds()


def _matches_action_evidence(
    left: StoredObservation, right: StoredObservation
) -> bool:
    if _matches(left, right):
        return True
    return (
        left.target_user_id is not None
        and left.target_user_id == right.target_user_id
        and "discord_ban_snapshot" in {left.source_kind, right.source_kind}
        and _transition_kind(left) == _transition_kind(right) == "ban"
    )


def _same_transition(
    left: StoredObservation,
    right: StoredObservation,
    ordered: list[StoredObservation],
) -> bool:
    transition = _transition_kind(left)
    if transition is None or transition != _transition_kind(right):
        return False
    lower, upper = sorted((_order_key(left), _order_key(right)))
    return not any(
        item.target_user_id == left.target_user_id
        and _transition_kind(item) not in {None, transition}
        and lower < _order_key(item) < upper
        for item in ordered
    )


def _best_attribution(
    observations: list[StoredObservation],
) -> tuple[int | None, str, str]:
    ranked = sorted(
        observations,
        key=lambda item: (
            ATTRIBUTION_SOURCE_PRIORITY.get(item.source_kind, 0),
            item.attribution_hint is not None,
            item.credited_moderator_hint is not None,
        ),
        reverse=True,
    )
    for item in ranked:
        kind = item.attribution_hint
        moderator = item.credited_moderator_hint
        if kind == "automation":
            return None, "automation", "explicit"
        if kind in {"human_direct", "automation_assisted"} and moderator is not None:
            confidence = "explicit" if item.source_kind == "red_modlog" else "audit"
            return moderator, kind, confidence
        if kind in {"human_direct", "automation_assisted"} and item.executor_user_id is not None:
            return item.executor_user_id, kind, "audit"
    return None, "unknown", "unknown"


def _project_action(
    anchor: StoredObservation,
    evidence: list[StoredObservation],
    action_kind: str | None = None,
) -> ProjectedAction:
    observations = [anchor, *evidence]
    moderator, attribution, confidence = _best_attribution(observations)
    richest = sorted(
        observations,
        key=lambda item: (
            item.source_kind == "red_modlog",
            item.reason is not None,
            item.expiry_at is not None,
        ),
        reverse=True,
    )[0]
    kind = action_kind or anchor.action_hint
    variant = (
        "hackban" if any(item.action_hint == "hackban" for item in observations) else None
    )
    if kind == "tempban":
        variant = "tempban"
    ended_at = next(
        (
            item.occurred_at
            for item in observations
            if item.action_hint == "unban" and item.occurred_at is not None
        ),
        None,
    )
    current_state = None
    if kind in {"ban", "tempban"}:
        current_state = "active"
    elif kind in {"softban", "unban"}:
        current_state = "ended"
    return ProjectedAction(
        action_kind=kind,
        action_variant=variant,
        target_user_id=anchor.target_user_id,
        moderator_user_id=moderator,
        attribution_kind=attribution,
        attribution_confidence=confidence,
        occurred_at=next(
            (item.occurred_at for item in [richest, *observations] if item.occurred_at),
            None,
        ),
        expiry_at=next((item.expiry_at for item in observations if item.expiry_at), None),
        ended_at=ended_at,
        reason=next(
            (item.reason for item in [richest, *observations] if item.reason), None
        ),
        current_state=current_state,
        observation_ids=tuple(sorted(item.observation_id for item in observations)),
    )


def _apply_lifecycle(actions: list[ProjectedAction]) -> list[ProjectedAction]:
    projected = list(actions)
    active_by_target: dict[int, int] = {}
    indices = sorted(
        range(len(projected)),
        key=lambda index: (
            projected[index].occurred_at or datetime.max.replace(tzinfo=timezone.utc),
            index,
        ),
    )
    for index in indices:
        action = projected[index]
        target_user_id = action.target_user_id
        if target_user_id is None:
            continue
        if action.action_kind in {"ban", "tempban"}:
            previous = active_by_target.get(target_user_id)
            if previous is not None:
                projected[previous] = replace(
                    projected[previous],
                    current_state="unknown",
                )
            active_by_target[target_user_id] = index
        elif action.action_kind == "unban":
            previous = active_by_target.pop(target_user_id, None)
            if previous is not None:
                projected[previous] = replace(
                    projected[previous],
                    current_state="ended",
                    ended_at=action.occurred_at,
                )
    return projected


def project_actions(observations: list[StoredObservation]) -> list[ProjectedAction]:
    ordered = sorted(
        observations,
        key=_order_key,
    )
    used: set[int] = set()
    actions: list[ProjectedAction] = []

    for softban in (item for item in ordered if item.action_hint == "softban"):
        evidence = [
            item
            for item in ordered
            if item.observation_id not in used
            and item.action_hint in {"ban", "unban"}
            and _matches(softban, item)
        ]
        closest_by_kind: list[StoredObservation] = []
        for kind in ("ban", "unban"):
            candidates = [item for item in evidence if item.action_hint == kind]
            if candidates:
                closest_by_kind.append(min(candidates, key=lambda item: _time_distance(softban, item) or 0))
        used.add(softban.observation_id)
        used.update(item.observation_id for item in closest_by_kind)
        actions.append(_project_action(softban, closest_by_kind, "softban"))

    for anchor in ordered:
        if anchor.observation_id in used:
            continue
        if anchor.action_hint not in {"ban", "hackban", "tempban", "unban"}:
            continue
        compatible = {"ban", "hackban", "tempban"} if anchor.action_hint != "unban" else {"unban"}
        candidates = [
            item
            for item in ordered
            if item.observation_id not in used
            and item.observation_id != anchor.observation_id
            and item.action_hint in compatible
            and (
                item.source_kind != anchor.source_kind
                or anchor.source_kind
                in {"discord_gateway", "discord_ban_snapshot"}
            )
            and _matches_action_evidence(anchor, item)
            and _same_transition(anchor, item, ordered)
        ]
        evidence = []
        for source_kind in ("red_modlog", "discord_audit", "discord_gateway", "discord_ban_snapshot"):
            source_candidates = [item for item in candidates if item.source_kind == source_kind]
            if source_candidates:
                if source_kind in {"discord_gateway", "discord_ban_snapshot"}:
                    evidence.extend(source_candidates)
                else:
                    evidence.append(
                        min(
                            source_candidates,
                            key=lambda item: _time_distance(anchor, item) or 0,
                        )
                    )
        used.add(anchor.observation_id)
        used.update(item.observation_id for item in evidence)
        action_kind = "tempban" if any(item.action_hint == "tempban" for item in [anchor, *evidence]) else anchor.action_hint
        if action_kind == "hackban":
            action_kind = "ban"
        actions.append(_project_action(anchor, evidence, action_kind))

    return _apply_lifecycle(actions)
