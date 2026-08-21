from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DiscordRoleSnapshot:
    snapshot_at: str
    gate_tiers: dict[int, int]
    gate_distribution: tuple[int, ...]
    duplicate_gate_users: tuple[int, ...]
    boolean_users: dict[str, tuple[int, ...]]
    role_holders: dict[int, tuple[int, ...]] = field(default_factory=dict)
    cached_member_count: int = 0
    reported_member_count: int = 0

    @property
    def affected_users(self) -> tuple[int, ...]:
        user_ids = set(self.gate_tiers)
        for holders in self.boolean_users.values():
            user_ids.update(holders)
        return tuple(sorted(user_ids))

    @property
    def proofless_awards(self) -> int:
        return sum(self.gate_tiers.values()) + sum(
            len(holders) for holders in self.boolean_users.values()
        )

    def render_initialization_summary(self) -> str:
        distribution = "\n".join(
            f"Tier {tier}: {count}"
            for tier, count in enumerate(self.gate_distribution, start=1)
        )
        boolean_count = sum(len(holders) for holders in self.boolean_users.values())
        return (
            "Achievement initialization plan\n\n"
            f"Role analytics snapshot: {self.snapshot_at}\n"
            f"Gate holders: {len(self.gate_tiers)}\n"
            f"Solo Gater holders: {boolean_count}\n"
            f"Duplicate Gate-role users: {len(self.duplicate_gate_users)}\n"
            f"Proofless achievements to create: {self.proofless_awards}\n"
            f"Users affected: {len(self.affected_users)}\n\n"
            f"Gate tier distribution:\n{distribution}\n\n"
            "Type `confirm` to continue"
        )


@dataclass(frozen=True, slots=True)
class DiscordPriorityPlan:
    snapshot: DiscordRoleSnapshot
    gate_users_changed: int
    boolean_grants: int
    boolean_revocations: int
    affected_users: tuple[int, ...]

    def render_summary(self) -> str:
        return (
            "Discord-priority achievement sync plan\n\n"
            f"Role analytics snapshot: {self.snapshot.snapshot_at}\n"
            f"Gate users to update: {self.gate_users_changed}\n"
            f"Proofless achievements to create: {self.boolean_grants}\n"
            f"Active achievements to revoke: {self.boolean_revocations}\n"
            f"Users affected: {len(self.affected_users)}\n\n"
            "Type `confirm` to continue"
        )


def build_discord_role_backup(
    *,
    guild_id: int,
    snapshot_at: str,
    cached_member_count: int,
    reported_member_count: int,
    role_holders: Mapping[int, tuple[int, ...]],
    user_names: Mapping[int, tuple[str, str]],
) -> bytes:
    tracked_role_ids = sorted(role_holders)
    role_ids_by_user: dict[int, list[int]] = {}
    for role_id in tracked_role_ids:
        for user_id in role_holders[role_id]:
            role_ids_by_user.setdefault(user_id, []).append(role_id)

    rows = [
        {
            "type": "metadata",
            "guild_id": guild_id,
            "snapshot_at": snapshot_at,
            "cached_member_count": cached_member_count,
            "reported_member_count": reported_member_count,
            "tracked_role_ids": tracked_role_ids,
        }
    ]
    for user_id in sorted(role_ids_by_user):
        username, display_name = user_names[user_id]
        rows.append(
            {
                "type": "member",
                "user_id": user_id,
                "username": username,
                "display_name": display_name,
                "role_ids": role_ids_by_user[user_id],
            }
        )
    jsonl = "".join(
        f"{json.dumps(row, sort_keys=True, separators=(',', ':'))}\n" for row in rows
    ).encode("utf-8")
    return gzip.compress(jsonl, mtime=0)


def build_discord_role_snapshot(
    *,
    snapshot_at: str,
    users_by_gate_role: tuple[tuple[int, ...], ...],
    boolean_users: Mapping[str, tuple[int, ...]],
    role_holders: Mapping[int, tuple[int, ...]] | None = None,
    cached_member_count: int = 0,
    reported_member_count: int = 0,
) -> DiscordRoleSnapshot:
    gate_tiers: dict[int, int] = {}
    gate_role_counts: dict[int, int] = {}
    for tier, user_ids in enumerate(users_by_gate_role, start=1):
        for user_id in user_ids:
            gate_tiers[user_id] = tier
            gate_role_counts[user_id] = gate_role_counts.get(user_id, 0) + 1
    return DiscordRoleSnapshot(
        snapshot_at=snapshot_at,
        gate_tiers=gate_tiers,
        gate_distribution=tuple(len(user_ids) for user_ids in users_by_gate_role),
        duplicate_gate_users=tuple(
            sorted(user_id for user_id, count in gate_role_counts.items() if count > 1)
        ),
        boolean_users={key: tuple(user_ids) for key, user_ids in boolean_users.items()},
        role_holders=(
            {role_id: tuple(user_ids) for role_id, user_ids in role_holders.items()}
            if role_holders is not None
            else {}
        ),
        cached_member_count=cached_member_count,
        reported_member_count=reported_member_count,
    )


def build_discord_priority_plan(
    snapshot: DiscordRoleSnapshot,
    *,
    stored_gate_tiers: Mapping[int, int],
    stored_boolean_users: Mapping[str, tuple[int, ...]],
) -> DiscordPriorityPlan:
    changed_users: set[int] = set()
    gate_users_changed = 0
    for user_id in set(stored_gate_tiers) | set(snapshot.gate_tiers):
        if stored_gate_tiers.get(user_id, 0) == snapshot.gate_tiers.get(user_id, 0):
            continue
        gate_users_changed += 1
        changed_users.add(user_id)

    boolean_grants = 0
    boolean_revocations = 0
    for achievement_key, discord_holders in snapshot.boolean_users.items():
        discord_users = set(discord_holders)
        stored_users = set(stored_boolean_users.get(achievement_key, ()))
        grants = discord_users - stored_users
        revocations = stored_users - discord_users
        boolean_grants += len(grants)
        boolean_revocations += len(revocations)
        changed_users.update(grants)
        changed_users.update(revocations)
    return DiscordPriorityPlan(
        snapshot=snapshot,
        gate_users_changed=gate_users_changed,
        boolean_grants=boolean_grants,
        boolean_revocations=boolean_revocations,
        affected_users=tuple(sorted(changed_users)),
    )
