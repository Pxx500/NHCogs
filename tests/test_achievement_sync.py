import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "NHMisc" / "achievement_sync.py"
SPEC = importlib.util.spec_from_file_location("_achievement_sync", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load achievement sync")
achievement_sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = achievement_sync
SPEC.loader.exec_module(achievement_sync)

build_discord_role_snapshot = achievement_sync.build_discord_role_snapshot
build_discord_priority_plan = achievement_sync.build_discord_priority_plan


def test_snapshot_uses_highest_gate_tier_and_reports_duplicates():
    snapshot = build_discord_role_snapshot(
        snapshot_at="2026-08-04T12:00:00+00:00",
        users_by_gate_role=((10,), (), (10, 11), (), (), ()),
        boolean_users={"solo_gater": (11,)},
    )

    assert snapshot.gate_tiers == {10: 3, 11: 3}
    assert snapshot.gate_distribution == (1, 0, 2, 0, 0, 0)
    assert snapshot.duplicate_gate_users == (10,)
    assert snapshot.affected_users == (10, 11)
    assert snapshot.proofless_awards == 7


def test_initialization_summary_omits_redundant_role_state_explanations():
    snapshot = build_discord_role_snapshot(
        snapshot_at="2026-08-04T12:00:00+00:00",
        users_by_gate_role=((10,), (), (), (), (), ()),
        boolean_users={"solo_gater": ()},
    )

    summary = snapshot.render_initialization_summary()

    assert "Role analytics snapshot: 2026-08-04T12:00:00+00:00" in summary
    assert "Gate holders: 1" in summary
    assert "No roles or achievements have been changed" not in summary
    assert "source of truth" not in summary


def test_discord_priority_plan_counts_grants_revocations_and_gate_changes():
    snapshot = build_discord_role_snapshot(
        snapshot_at="2026-08-04T12:00:00+00:00",
        users_by_gate_role=((10,), (11,), (), (), (), ()),
        boolean_users={"solo_gater": (11, 12)},
    )

    plan = build_discord_priority_plan(
        snapshot,
        stored_gate_tiers={10: 2, 13: 1},
        stored_boolean_users={"solo_gater": (10, 11)},
    )

    assert plan.gate_users_changed == 3
    assert plan.boolean_grants == 1
    assert plan.boolean_revocations == 1
    assert plan.affected_users == (10, 11, 12, 13)
