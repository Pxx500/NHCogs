from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

GATE_TIER_ROLE_IDS = (
    798700443979087892,
    1004822424921055233,
    1097204292198338692,
    1442209801374269682,
    1437811360208781406,
    1522017144878137385,
)
SINGLEPLAYER_GATE_COMPLETED_ROLE_ID = 1442208051212976158


@dataclass(frozen=True, slots=True)
class GateRoleTransition:
    current_role_ids: tuple[int, ...]
    current_tier: int | None
    target_tier: int | None
    duplicate_roles: bool

    @property
    def target_role_id(self) -> int | None:
        if self.target_tier is None:
            return None
        return GATE_TIER_ROLE_IDS[self.target_tier - 1]


def plan_gate_transition(member_role_ids: Iterable[int]) -> GateRoleTransition:
    member_role_id_set = set(member_role_ids)
    current_role_ids = tuple(
        role_id for role_id in GATE_TIER_ROLE_IDS if role_id in member_role_id_set
    )
    current_tier = (
        GATE_TIER_ROLE_IDS.index(current_role_ids[-1]) + 1
        if current_role_ids
        else None
    )
    target_tier = (
        1
        if current_tier is None
        else current_tier + 1
        if current_tier < len(GATE_TIER_ROLE_IDS)
        else None
    )
    return GateRoleTransition(
        current_role_ids=current_role_ids,
        current_tier=current_tier,
        target_tier=target_tier,
        duplicate_roles=len(current_role_ids) > 1,
    )


def build_desired_role_ids(
    member_role_ids: Iterable[int], transition: GateRoleTransition
) -> tuple[int, ...]:
    if transition.target_role_id is None:
        return tuple(member_role_ids)
    return build_role_ids_for_target(member_role_ids, transition.target_role_id)


def build_role_ids_for_target(
    member_role_ids: Iterable[int], target_role_id: int
) -> tuple[int, ...]:
    desired_role_ids = tuple(
        role_id for role_id in member_role_ids if role_id not in GATE_TIER_ROLE_IDS
    )
    return (*desired_role_ids, target_role_id)
