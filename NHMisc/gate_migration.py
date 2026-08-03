from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass

LEGACY_SP_ROLE_IDS = (
    1348078501986828461,
    1348078496710135888,
    1348078483384958986,
    1442209676530815076,
    1442208051212976158,
)
LEGACY_MP_ROLE_IDS = (
    798700443979087892,
    1004822424921055233,
    1097204292198338692,
    1442209801374269682,
    1437811360208781406,
)
TARGET_TIER_ROLE_IDS = (
    798700443979087892,
    1004822424921055233,
    1097204292198338692,
    1442209801374269682,
    1437811360208781406,
    1522017144878137385,
    1348078501986828461,
    1348078496710135888,
    1348078483384958986,
    1442209676530815076,
)
SINGLEPLAYER_COMPLETED_ROLE_ID = 1442208051212976158
LEGACY_GATE_ROLE_IDS = frozenset((*LEGACY_SP_ROLE_IDS, *LEGACY_MP_ROLE_IDS))
ALL_GATE_ROLE_IDS = frozenset((*TARGET_TIER_ROLE_IDS, SINGLEPLAYER_COMPLETED_ROLE_ID))


@dataclass(frozen=True)
class MemberSnapshot:
    user_id: int
    username: str
    role_ids: tuple[int, ...]


@dataclass(frozen=True)
class MemberMigrationPlan:
    snapshot: MemberSnapshot
    sp_count: int
    mp_count: int
    target_tier: int
    target_gate_role_ids: frozenset[int]
    duplicate_sp_role_ids: tuple[int, ...]
    duplicate_mp_role_ids: tuple[int, ...]
    original_gate_role_ids: frozenset[int] = frozenset()
    unexpected_role_ids: tuple[int, ...] = ()
    changed: bool = False


@dataclass(frozen=True)
class MigrationPlan:
    members: tuple[MemberMigrationPlan, ...]


@dataclass(frozen=True)
class MigrationSummary:
    total_members: int
    role_memberships: int
    legacy_members: int
    changed_members: int
    unchanged_members: int
    singleplayer_completed_members: int
    duplicate_sp_members: int
    duplicate_mp_members: int
    unexpected_members: int
    source_sp_tiers: tuple[int, ...]
    source_mp_tiers: tuple[int, ...]
    target_tiers: tuple[int, ...]


@dataclass(frozen=True)
class BackupPart:
    filename: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class BackupBundle:
    snapshot_sha256: str
    parts: tuple[BackupPart, ...]
    manifest: BackupPart | None


class BackupRowTooLargeError(ValueError):
    pass


class BackupVerificationError(ValueError):
    pass


def plan_member(snapshot: MemberSnapshot) -> MemberMigrationPlan:
    role_ids = frozenset(snapshot.role_ids)
    sp_roles = tuple(role_id for role_id in LEGACY_SP_ROLE_IDS if role_id in role_ids)
    mp_roles = tuple(role_id for role_id in LEGACY_MP_ROLE_IDS if role_id in role_ids)
    sp_count = max(
        (level for level, role_id in enumerate(LEGACY_SP_ROLE_IDS, start=1) if role_id in role_ids),
        default=0,
    )
    mp_count = max(
        (level for level, role_id in enumerate(LEGACY_MP_ROLE_IDS, start=1) if role_id in role_ids),
        default=0,
    )
    target_tier = sp_count + mp_count
    target_gate_role_ids = set()
    if target_tier:
        target_gate_role_ids.add(TARGET_TIER_ROLE_IDS[target_tier - 1])
    if sp_count:
        target_gate_role_ids.add(SINGLEPLAYER_COMPLETED_ROLE_ID)
    original_gate_role_ids = role_ids & ALL_GATE_ROLE_IDS
    unexpected_role_ids = tuple(sorted(original_gate_role_ids - LEGACY_GATE_ROLE_IDS))
    frozen_target_role_ids = frozenset(target_gate_role_ids)

    return MemberMigrationPlan(
        snapshot=snapshot,
        sp_count=sp_count,
        mp_count=mp_count,
        target_tier=target_tier,
        target_gate_role_ids=frozen_target_role_ids,
        duplicate_sp_role_ids=sp_roles if len(sp_roles) > 1 else (),
        duplicate_mp_role_ids=mp_roles if len(mp_roles) > 1 else (),
        original_gate_role_ids=original_gate_role_ids,
        unexpected_role_ids=unexpected_role_ids,
        changed=original_gate_role_ids != frozen_target_role_ids,
    )


def plan_migration(snapshots: tuple[MemberSnapshot, ...]) -> MigrationPlan:
    ordered_snapshots = sorted(snapshots, key=lambda snapshot: snapshot.user_id)
    return MigrationPlan(
        members=tuple(plan_member(snapshot) for snapshot in ordered_snapshots)
    )


def summarize_plan(plan: MigrationPlan) -> MigrationSummary:
    members = plan.members

    def tier_counts(attribute: str, tier_count: int) -> tuple[int, ...]:
        counts = [0] * tier_count
        for member in members:
            tier = int(getattr(member, attribute))
            if tier:
                counts[tier - 1] += 1
        return tuple(counts)

    changed_members = sum(member.changed for member in members)
    return MigrationSummary(
        total_members=len(members),
        role_memberships=sum(len(member.snapshot.role_ids) for member in members),
        legacy_members=sum(
            bool(member.original_gate_role_ids & LEGACY_GATE_ROLE_IDS)
            for member in members
        ),
        changed_members=changed_members,
        unchanged_members=len(members) - changed_members,
        singleplayer_completed_members=sum(member.sp_count > 0 for member in members),
        duplicate_sp_members=sum(bool(member.duplicate_sp_role_ids) for member in members),
        duplicate_mp_members=sum(bool(member.duplicate_mp_role_ids) for member in members),
        unexpected_members=sum(bool(member.unexpected_role_ids) for member in members),
        source_sp_tiers=tier_counts("sp_count", len(LEGACY_SP_ROLE_IDS)),
        source_mp_tiers=tier_counts("mp_count", len(LEGACY_MP_ROLE_IDS)),
        target_tiers=tier_counts("target_tier", len(TARGET_TIER_ROLE_IDS)),
    )


def build_backup(
    snapshots: tuple[MemberSnapshot, ...],
    *,
    guild_id: int,
    run_id: str,
    max_part_size: int,
) -> BackupBundle:
    if max_part_size <= 0:
        raise ValueError("max_part_size must be positive")
    ordered_snapshots = sorted(snapshots, key=lambda snapshot: snapshot.user_id)
    row_payloads = []
    for snapshot in ordered_snapshots:
        row = {
            "role_ids": [str(role_id) for role_id in sorted(snapshot.role_ids)],
            "user_id": str(snapshot.user_id),
            "username": snapshot.username,
        }
        row_payloads.append(
            (
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode()
        )
    payload = b"".join(row_payloads)
    compressed = gzip.compress(payload, mtime=0)
    snapshot_sha256 = hashlib.sha256(payload).hexdigest()
    base_filename = f"gate-migration-{run_id}"
    if len(compressed) <= max_part_size:
        part = _backup_part(f"{base_filename}.jsonl.gz", compressed)
        return BackupBundle(
            snapshot_sha256=snapshot_sha256,
            parts=(part,),
            manifest=None,
        )

    compressed_chunks = []
    start = 0
    while start < len(row_payloads):
        low = start + 1
        high = len(row_payloads)
        best_end = None
        best_payload = None
        while low <= high:
            end = (low + high) // 2
            candidate = gzip.compress(b"".join(row_payloads[start:end]), mtime=0)
            if len(candidate) <= max_part_size:
                best_end = end
                best_payload = candidate
                low = end + 1
            else:
                high = end - 1
        if best_end is None or best_payload is None:
            raise BackupRowTooLargeError("One backup row exceeds the upload limit")
        compressed_chunks.append(best_payload)
        start = best_end

    part_count = len(compressed_chunks)
    parts = tuple(
        _backup_part(
            f"{base_filename}.part-{index:03d}-of-{part_count:03d}.jsonl.gz",
            chunk,
        )
        for index, chunk in enumerate(compressed_chunks, start=1)
    )
    manifest_payload = (
        json.dumps(
            {
                "format": "nhmisc-gate-role-backup",
                "guild_id": str(guild_id),
                "parts": [
                    {
                        "filename": part.filename,
                        "sha256": part.sha256,
                        "size": len(part.data),
                    }
                    for part in parts
                ],
                "run_id": run_id,
                "snapshot_sha256": snapshot_sha256,
                "version": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    return BackupBundle(
        snapshot_sha256=snapshot_sha256,
        parts=parts,
        manifest=_backup_part(f"{base_filename}.manifest.json", manifest_payload),
    )


def _backup_part(filename: str, data: bytes) -> BackupPart:
    return BackupPart(
        filename=filename,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def verify_backup(bundle: BackupBundle) -> None:
    payloads = []
    for part in bundle.parts:
        if hashlib.sha256(part.data).hexdigest() != part.sha256:
            raise BackupVerificationError(f"Backup part hash mismatch: {part.filename}")
        try:
            payloads.append(gzip.decompress(part.data))
        except OSError as error:
            raise BackupVerificationError(
                f"Backup part is not valid gzip: {part.filename}"
            ) from error
    if hashlib.sha256(b"".join(payloads)).hexdigest() != bundle.snapshot_sha256:
        raise BackupVerificationError("Backup snapshot hash mismatch")

    if bundle.manifest is None:
        if len(bundle.parts) != 1:
            raise BackupVerificationError("Multipart backup is missing its manifest")
        return
    if hashlib.sha256(bundle.manifest.data).hexdigest() != bundle.manifest.sha256:
        raise BackupVerificationError("Backup manifest hash mismatch")
    try:
        manifest = json.loads(bundle.manifest.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupVerificationError("Backup manifest is invalid") from error
    expected_parts = [
        {
            "filename": part.filename,
            "sha256": part.sha256,
            "size": len(part.data),
        }
        for part in bundle.parts
    ]
    if (
        manifest.get("snapshot_sha256") != bundle.snapshot_sha256
        or manifest.get("parts") != expected_parts
    ):
        raise BackupVerificationError("Backup manifest does not match its parts")
