from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .gate_roles import SINGLEPLAYER_GATE_COMPLETED_ROLE_ID

STARGATE_COMPLETED_KEY = "stargate_completed"
SOLO_GATER_KEY = "solo_gater"


class AchievementKind(str, Enum):
    BOOLEAN = "boolean"
    REPEATABLE = "repeatable"


@dataclass(frozen=True, slots=True)
class AchievementDefinition:
    key: str
    display_name: str
    kind: AchievementKind
    role_id: int | None = None
    grantable: bool = True
    revocable: bool = True


ACHIEVEMENT_DEFINITIONS = (
    AchievementDefinition(
        key=STARGATE_COMPLETED_KEY,
        display_name="Stargate completed",
        kind=AchievementKind.REPEATABLE,
        grantable=False,
        revocable=False,
    ),
    AchievementDefinition(
        key=SOLO_GATER_KEY,
        display_name="Solo Gater",
        kind=AchievementKind.BOOLEAN,
        role_id=SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
    ),
)

ACHIEVEMENTS_BY_KEY = {
    definition.key: definition for definition in ACHIEVEMENT_DEFINITIONS
}
GRANTABLE_BOOLEAN_ACHIEVEMENTS = tuple(
    definition
    for definition in ACHIEVEMENT_DEFINITIONS
    if definition.kind is AchievementKind.BOOLEAN and definition.grantable
)
REVOCABLE_BOOLEAN_ACHIEVEMENTS = tuple(
    definition
    for definition in ACHIEVEMENT_DEFINITIONS
    if definition.kind is AchievementKind.BOOLEAN and definition.revocable
)
