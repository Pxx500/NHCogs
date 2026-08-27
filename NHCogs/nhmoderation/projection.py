from __future__ import annotations

from datetime import timedelta

from .models import ProjectedAction, StoredObservation

CORRELATION_WINDOW = timedelta(minutes=5)
PROJECTION_VERSION = 1


def _time_distance(left: StoredObservation, right: StoredObservation) -> float | None:
    left_time = left.occurred_at or left.observed_at
    right_time = right.occurred_at or right.observed_at
    return abs((left_time - right_time).total_seconds())


def _matches(left: StoredObservation, right: StoredObservation) -> bool:
    if left.target_user_id is None or left.target_user_id != right.target_user_id:
        return False
    distance = _time_distance(left, right)
    return distance is not None and distance <= CORRELATION_WINDOW.total_seconds()


def _best_attribution(
    observations: list[StoredObservation],
) -> tuple[int | None, str, str]:
    ranked = sorted(
        observations,
        key=lambda item: (
            item.attribution_hint is not None,
            item.credited_moderator_hint is not None,
            item.source_kind == "red_modlog",
            item.source_kind == "discord_audit",
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
        reason=next(
            (item.reason for item in [richest, *observations] if item.reason), None
        ),
        current_state="active" if anchor.source_kind == "discord_ban_snapshot" else None,
        observation_ids=tuple(sorted(item.observation_id for item in observations)),
    )


def project_actions(observations: list[StoredObservation]) -> list[ProjectedAction]:
    ordered = sorted(
        observations,
        key=lambda item: (item.occurred_at or item.observed_at, item.observation_id),
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
                or anchor.source_kind == "discord_gateway"
            )
            and _matches(anchor, item)
        ]
        evidence = []
        for source_kind in ("red_modlog", "discord_audit", "discord_gateway", "discord_ban_snapshot"):
            source_candidates = [item for item in candidates if item.source_kind == source_kind]
            if source_candidates:
                if source_kind == "discord_gateway":
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

    return actions
