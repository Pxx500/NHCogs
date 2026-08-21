from __future__ import annotations

from .achievement_store import AchievementDefinition, AchievementKind
from .gate_roles import SINGLEPLAYER_GATE_COMPLETED_ROLE_ID

STARGATE_COMPLETED_KEY = "stargate_completed"
SOLO_GATER_KEY = "solo_gater"


STARGATE_DEFINITION = AchievementDefinition(
    key=STARGATE_COMPLETED_KEY,
    display_name="Stargate completed",
    kind=AchievementKind.REPEATABLE,
    grantable=False,
    revocable=False,
    display_order=0,
)
SOLO_GATER_DEFINITION = AchievementDefinition(
    key=SOLO_GATER_KEY,
    display_name="Solo Gater",
    kind=AchievementKind.BOOLEAN,
    role_id=SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
    display_order=1,
)

ACHIEVEMENT_DEFINITIONS = (STARGATE_DEFINITION, SOLO_GATER_DEFINITION)

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
