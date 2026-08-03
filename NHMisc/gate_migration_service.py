from __future__ import annotations

import asyncio
import csv
import hashlib
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .gate_migration import (
    ALL_GATE_ROLE_IDS,
    LEGACY_MP_ROLE_IDS,
    LEGACY_SP_ROLE_IDS,
    SINGLEPLAYER_COMPLETED_ROLE_ID,
    TARGET_TIER_ROLE_IDS,
    BackupBundle,
    BackupPart,
    MemberMigrationPlan,
    MemberSnapshot,
    MigrationPlan,
    MigrationSummary,
    build_backup,
    plan_migration,
    summarize_plan,
    verify_backup,
)
from .gate_migration_store import (
    ActiveMigrationExistsError,
    CompletionReceipt,
    MemberStatus,
    RestoreStatus,
    RunState,
    SchemaState,
    StoredArtifact,
)
from .role_analytics_service import FullMemberRequestCooldownError

HTTP_FORBIDDEN = 403
APPLY_PROGRESS_INTERVAL = 1000


class MigrationPreflightError(RuntimeError):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


@dataclass(frozen=True)
class SnapshotCollection:
    plan: MigrationPlan
    summary: MigrationSummary
    sync_result: Any


@dataclass(frozen=True)
class LegacyAuditResult:
    members: tuple[MemberMigrationPlan, ...]
    csv_data: bytes
    sync_result: Any


@dataclass(frozen=True)
class PreparedMigration:
    run: Any
    collection: SnapshotCollection
    backup: BackupBundle


@dataclass(frozen=True)
class PublishedPreparation:
    artifact_message_ids: tuple[int, ...]
    summary_message_ids: tuple[int, ...]
    pages: tuple[str, ...]


@dataclass(frozen=True)
class ApplyResult:
    completed: int
    departed: int
    skipped_unmodifiable: int
    tier_role_counts: tuple[int, ...]


@dataclass(frozen=True)
class ApplyProgress:
    processed: int
    remaining: int
    completed: int
    departed: int
    failed: int
    skipped_unmodifiable: int


@dataclass(frozen=True)
class VerifyResult:
    matched: int
    departed: int


@dataclass(frozen=True)
class RestoreResult:
    completed: int
    departed: int
    skipped_unmodifiable: int


@dataclass(frozen=True)
class MigrationStatus:
    run: Any
    schema_state: SchemaState
    member_counts: dict[MemberStatus, int]
    restore_counts: dict[RestoreStatus, int]
    target_distribution: tuple[int, ...]
    backup_verified: bool
    latest_failure: str | None


class GateMigrationService:
    def __init__(self, bot: Any, analytics: Any, store: Any) -> None:
        self._bot = bot
        self._analytics = analytics
        self._store = store

    async def collect_snapshot(self, guild: Any) -> SnapshotCollection:
        self._validate_guild(guild)
        sync_result = await self._analytics.sync_guild(
            guild, manual=True, force_fresh=True
        )
        default_role_id = int(guild.default_role.id)
        snapshots = tuple(
            MemberSnapshot(
                user_id=int(member.id),
                username=str(member.name),
                role_ids=tuple(
                    sorted(
                        int(role.id)
                        for role in member.roles
                        if int(role.id) != default_role_id
                    )
                ),
            )
            for member in tuple(guild.members)
        )
        plan = plan_migration(snapshots)
        return SnapshotCollection(
            plan=plan,
            summary=summarize_plan(plan),
            sync_result=sync_result,
        )

    async def audit_legacy_users(self, guild: Any) -> LegacyAuditResult:
        collection = await self.collect_snapshot(guild)
        conflicts = tuple(
            member
            for member in collection.plan.members
            if member.duplicate_sp_role_ids or member.duplicate_mp_role_ids
        )
        return LegacyAuditResult(
            members=conflicts,
            csv_data=self._legacy_audit_csv(conflicts),
            sync_result=collection.sync_result,
        )

    async def prepare_run(
        self,
        guild: Any,
        *,
        run_id: str,
        operator_id: int,
        channel_id: int,
        created_at: str,
        max_part_size: int,
    ) -> PreparedMigration:
        schema_state = await self._store.get_schema_state(int(guild.id))
        if schema_state is not SchemaState.LEGACY:
            raise MigrationPreflightError(
                "migration_already_started",
                "The gate migration has already started on this server",
            )
        if await self._store.get_active_run(int(guild.id)) is not None:
            raise MigrationPreflightError(
                "active_migration", "A gate migration is already active"
            )
        collection = await self.collect_snapshot(guild)
        backup = build_backup(
            tuple(member.snapshot for member in collection.plan.members),
            guild_id=int(guild.id),
            run_id=run_id,
            max_part_size=max_part_size,
        )
        verify_backup(backup)
        try:
            run = await self._store.create_run(
                run_id=run_id,
                guild_id=int(guild.id),
                operator_id=operator_id,
                channel_id=channel_id,
                created_at=created_at,
                snapshot_sha256=backup.snapshot_sha256,
                plan=collection.plan,
            )
        except ActiveMigrationExistsError as error:
            raise MigrationPreflightError(
                "migration_already_started",
                "The gate migration has already started on this server",
            ) from error
        return PreparedMigration(run=run, collection=collection, backup=backup)

    async def publish_preparation(
        self,
        prepared: PreparedMigration,
        *,
        send_attachment: Callable[[str, bytes], Awaitable[Any]],
        send_text: Callable[[str], Awaitable[Any]],
    ) -> PublishedPreparation:
        artifact_message_ids = []
        artifact_specs = [
            ("backup_part", index, part)
            for index, part in enumerate(prepared.backup.parts, start=1)
        ]
        if prepared.backup.manifest is not None:
            artifact_specs.append(("manifest", 0, prepared.backup.manifest))
        anomaly_members = tuple(
            member
            for member in prepared.collection.plan.members
            if member.duplicate_sp_role_ids
            or member.duplicate_mp_role_ids
            or member.unexpected_role_ids
        )
        if anomaly_members:
            anomaly_data = self._legacy_audit_csv(anomaly_members)
            anomaly_filename = f"gate-migration-{prepared.run.run_id}-anomalies.csv"
            anomaly_part = self._plain_artifact(anomaly_filename, anomaly_data)
            artifact_specs.append(("anomaly", 0, anomaly_part))

        for kind, part_index, artifact in artifact_specs:
            message = await send_attachment(artifact.filename, artifact.data)
            message_id = int(message.id)
            await self._store.record_artifact(
                StoredArtifact(
                    run_id=prepared.run.run_id,
                    kind=kind,
                    part_index=part_index,
                    filename=artifact.filename,
                    sha256=artifact.sha256,
                    size=len(artifact.data),
                    channel_id=int(prepared.run.channel_id),
                    message_id=message_id,
                )
            )
            artifact_message_ids.append(message_id)

        pages = self._preparation_pages(prepared, artifact_specs)
        summary_message_ids = []
        for page in pages:
            message = await send_text(page)
            summary_message_ids.append(int(message.id))
        await self._store.transition_run(prepared.run.run_id, RunState.PREPARED)
        return PublishedPreparation(
            artifact_message_ids=tuple(artifact_message_ids),
            summary_message_ids=tuple(summary_message_ids),
            pages=pages,
        )

    async def apply_run(
        self,
        guild: Any,
        run_id: str,
        *,
        progress_callback: Callable[[ApplyProgress], Awaitable[None]] | None = None,
    ) -> ApplyResult:
        run = await self._store.get_run(run_id)
        if run is None or int(run.guild_id) != int(guild.id):
            raise MigrationPreflightError(
                "invalid_migration", "The gate migration does not belong to this server"
            )
        if run.state not in {
            RunState.PREPARED,
            RunState.CONFIRMED,
            RunState.APPLYING,
            RunState.APPLY_FAILED,
            RunState.APPLIED,
        }:
            raise MigrationPreflightError(
                "invalid_migration_state", "The gate migration is not ready to apply"
            )
        self._validate_guild(guild)
        stored_members = await self._store.get_members(run_id)
        self._validate_prepared_member_roles(guild, stored_members)
        if run.state is not RunState.APPLYING:
            await self._store.transition_run(run_id, RunState.APPLYING)
        for stored_member in stored_members:
            if (
                stored_member.status is MemberStatus.PENDING
                and not stored_member.plan.changed
                and guild.get_member(stored_member.plan.snapshot.user_id) is None
            ):
                await self._store.mark_unattempted_member_departed(
                    run_id,
                    stored_member.plan.snapshot.user_id,
                )
        await self._store.complete_unchanged_members(run_id)
        stored_members = await self._store.get_members(run_id)

        counts = dict.fromkeys(MemberStatus, 0)
        default_role_id = int(guild.default_role.id)
        total = len(stored_members)
        for processed, stored_member in enumerate(stored_members, start=1):
            outcome = await self._apply_member(
                guild, run_id, stored_member, default_role_id
            )
            counts[outcome] += 1
            if (
                progress_callback is not None
                and processed % APPLY_PROGRESS_INTERVAL == 0
                and processed < total
            ):
                await progress_callback(
                    ApplyProgress(
                        processed=processed,
                        remaining=total - processed,
                        completed=counts[MemberStatus.COMPLETED],
                        departed=counts[MemberStatus.DEPARTED],
                        failed=(
                            counts[MemberStatus.FAILED]
                            + counts[MemberStatus.CONFLICT]
                        ),
                        skipped_unmodifiable=counts[
                            MemberStatus.SKIPPED_UNMODIFIABLE
                        ],
                    )
                )

        try:
            await self._analytics.sync_guild(guild, manual=True, force_fresh=True)
        except FullMemberRequestCooldownError as error:
            await asyncio.sleep(error.retry_after)
            await self._analytics.sync_guild(guild, manual=True, force_fresh=True)
        tier_role_counts = self._tier_role_counts(guild)
        await self._store.transition_run(run_id, RunState.APPLIED)
        return ApplyResult(
            completed=counts[MemberStatus.COMPLETED],
            departed=counts[MemberStatus.DEPARTED],
            skipped_unmodifiable=counts[MemberStatus.SKIPPED_UNMODIFIABLE],
            tier_role_counts=tier_role_counts,
        )

    def _validate_prepared_member_roles(self, guild: Any, stored_members) -> None:
        for stored_member in stored_members:
            if stored_member.status is not MemberStatus.PENDING:
                continue
            member = guild.get_member(stored_member.plan.snapshot.user_id)
            if member is None:
                continue
            if self._gate_role_ids(member) != stored_member.plan.original_gate_role_ids:
                raise MigrationPreflightError(
                    "member_drift",
                    "A member's Gate roles changed since preparation",
                )

    async def _apply_member(
        self, guild: Any, run_id: str, stored_member: Any, default_role_id: int
    ) -> MemberStatus:
        if stored_member.status in {MemberStatus.COMPLETED, MemberStatus.DEPARTED}:
            return stored_member.status
        plan = stored_member.plan
        member = guild.get_member(plan.snapshot.user_id)
        if stored_member.status is not MemberStatus.IN_PROGRESS:
            await self._store.begin_member_attempt(run_id, plan.snapshot.user_id)
        if member is None:
            await self._store.set_member_status(
                run_id, plan.snapshot.user_id, MemberStatus.DEPARTED
            )
            return MemberStatus.DEPARTED

        live_gate_role_ids = self._gate_role_ids(member)
        if live_gate_role_ids == plan.target_gate_role_ids:
            await self._store.set_member_status(
                run_id, plan.snapshot.user_id, MemberStatus.COMPLETED
            )
            return MemberStatus.COMPLETED
        if live_gate_role_ids != plan.original_gate_role_ids:
            await self._store.set_member_status(
                run_id,
                plan.snapshot.user_id,
                MemberStatus.CONFLICT,
                error_code="member_drift",
            )
            await self._store.transition_run(run_id, RunState.APPLY_FAILED)
            raise MigrationPreflightError(
                "member_drift", "A member has unexpected Gate roles"
            )

        preserved_roles = [
            role
            for role in member.roles
            if int(role.id) != default_role_id
            and int(role.id) not in ALL_GATE_ROLE_IDS
        ]
        target_roles = [
            guild.get_role(role_id) for role_id in sorted(plan.target_gate_role_ids)
        ]
        try:
            await member.edit(
                roles=preserved_roles + target_roles,
                reason=f"Stargate migration {run_id}",
            )
        except Exception as error:
            return await self._handle_apply_error(guild, run_id, plan, error)
        await self._store.set_member_status(
            run_id, plan.snapshot.user_id, MemberStatus.COMPLETED
        )
        return MemberStatus.COMPLETED

    async def _handle_apply_error(
        self, guild: Any, run_id: str, plan: MemberMigrationPlan, error: Exception
    ) -> MemberStatus:
        if self._is_forbidden(error):
            try:
                self._validate_guild(guild)
            except MigrationPreflightError:
                await self._store.set_member_status(
                    run_id,
                    plan.snapshot.user_id,
                    MemberStatus.FAILED,
                    error_code="role_configuration_changed",
                )
                await self._store.transition_run(run_id, RunState.APPLY_FAILED)
                raise
            await self._store.set_member_status(
                run_id,
                plan.snapshot.user_id,
                MemberStatus.SKIPPED_UNMODIFIABLE,
                error_code="member_unmodifiable",
            )
            return MemberStatus.SKIPPED_UNMODIFIABLE
        await self._store.set_member_status(
            run_id,
            plan.snapshot.user_id,
            MemberStatus.FAILED,
            error_code="discord_api_error",
        )
        await self._store.transition_run(run_id, RunState.APPLY_FAILED)
        raise error

    async def verify_run(self, guild: Any, run_id: str) -> VerifyResult:
        run = await self._store.get_run(run_id)
        if (
            run is None
            or int(run.guild_id) != int(guild.id)
            or run.state is not RunState.APPLIED
        ):
            raise MigrationPreflightError(
                "invalid_migration_state", "The gate migration is not ready to verify"
            )
        self._validate_guild(guild)
        await self._analytics.sync_guild(guild, manual=True, force_fresh=True)
        stored_members = await self._store.get_members(run_id)
        unresolved = tuple(
            member
            for member in stored_members
            if member.status
            not in {MemberStatus.COMPLETED, MemberStatus.DEPARTED}
        )
        if unresolved:
            raise MigrationPreflightError(
                "unresolved_members",
                "The gate migration has unresolved members",
            )

        matched = 0
        departed = 0
        for stored_member in stored_members:
            member = guild.get_member(stored_member.plan.snapshot.user_id)
            if member is None or stored_member.status is MemberStatus.DEPARTED:
                departed += 1
                continue
            if self._gate_role_ids(member) != stored_member.plan.target_gate_role_ids:
                raise MigrationPreflightError(
                    "verification_mismatch",
                    "A member does not match the planned Gate roles",
                )
            matched += 1

        await self._store.transition_run(run_id, RunState.VERIFIED)
        return VerifyResult(matched=matched, departed=departed)

    async def restore_run(self, guild: Any, run_id: str) -> RestoreResult:
        run = await self._store.get_run(run_id)
        if (
            run is None
            or int(run.guild_id) != int(guild.id)
            or run.state
            not in {
                RunState.APPLY_FAILED,
                RunState.APPLIED,
                RunState.VERIFIED,
                RunState.RESTORING,
                RunState.RESTORE_FAILED,
            }
        ):
            raise MigrationPreflightError(
                "invalid_migration_state", "The gate migration is not ready to restore"
            )
        self._validate_guild(guild)
        if run.state is not RunState.RESTORING:
            await self._store.transition_run(run_id, RunState.RESTORING)

        counts = dict.fromkeys(RestoreStatus, 0)
        default_role_id = int(guild.default_role.id)
        for stored_member in await self._store.get_members(run_id):
            outcome = await self._restore_member(
                guild, run_id, stored_member, default_role_id
            )
            if outcome is not None:
                counts[outcome] += 1

        await self._analytics.sync_guild(guild, manual=True, force_fresh=True)
        final_state = (
            RunState.RESTORE_FAILED
            if counts[RestoreStatus.SKIPPED_UNMODIFIABLE]
            else RunState.RESTORED
        )
        await self._store.transition_run(run_id, final_state)
        return RestoreResult(
            completed=counts[RestoreStatus.COMPLETED],
            departed=counts[RestoreStatus.DEPARTED],
            skipped_unmodifiable=counts[RestoreStatus.SKIPPED_UNMODIFIABLE],
        )

    async def _restore_member(
        self, guild: Any, run_id: str, stored_member: Any, default_role_id: int
    ) -> RestoreStatus | None:
        if stored_member.attempts == 0:
            return None
        if stored_member.restore_status in {
            RestoreStatus.COMPLETED,
            RestoreStatus.DEPARTED,
        }:
            return stored_member.restore_status
        plan = stored_member.plan
        member = guild.get_member(plan.snapshot.user_id)
        if stored_member.restore_status is not RestoreStatus.IN_PROGRESS:
            await self._store.begin_restore_attempt(run_id, plan.snapshot.user_id)
        if member is None:
            await self._store.set_restore_status(
                run_id, plan.snapshot.user_id, RestoreStatus.DEPARTED
            )
            return RestoreStatus.DEPARTED

        desired_roles = self._restore_roles(guild, member, plan, default_role_id)
        live_role_ids = {
            int(role.id) for role in member.roles if int(role.id) != default_role_id
        }
        if live_role_ids != set(desired_roles):
            try:
                await member.edit(
                    roles=[desired_roles[role_id] for role_id in sorted(desired_roles)],
                    reason=f"Restore Stargate migration {run_id}",
                )
            except Exception as error:
                return await self._handle_restore_error(guild, run_id, plan, error)
        await self._store.set_restore_status(
            run_id, plan.snapshot.user_id, RestoreStatus.COMPLETED
        )
        return RestoreStatus.COMPLETED

    @staticmethod
    def _restore_roles(
        guild: Any, member: Any, plan: MemberMigrationPlan, default_role_id: int
    ) -> dict[int, Any]:
        desired_roles = {
            int(role.id): role
            for role in member.roles
            if int(role.id) != default_role_id
            and int(role.id) not in ALL_GATE_ROLE_IDS
        }
        backed_non_gate_role_ids = {
            role_id
            for role_id in plan.snapshot.role_ids
            if role_id not in ALL_GATE_ROLE_IDS
        }
        for role_id in backed_non_gate_role_ids | set(plan.original_gate_role_ids):
            role = guild.get_role(role_id)
            if role is not None:
                desired_roles[role_id] = role
        return desired_roles

    async def _handle_restore_error(
        self, guild: Any, run_id: str, plan: MemberMigrationPlan, error: Exception
    ) -> RestoreStatus:
        if self._is_forbidden(error):
            try:
                self._validate_guild(guild)
            except MigrationPreflightError:
                await self._store.set_restore_status(
                    run_id,
                    plan.snapshot.user_id,
                    RestoreStatus.FAILED,
                    error_code="role_configuration_changed",
                )
                await self._store.transition_run(run_id, RunState.RESTORE_FAILED)
                raise
            await self._store.set_restore_status(
                run_id,
                plan.snapshot.user_id,
                RestoreStatus.SKIPPED_UNMODIFIABLE,
                error_code="member_unmodifiable",
            )
            return RestoreStatus.SKIPPED_UNMODIFIABLE
        await self._store.set_restore_status(
            run_id,
            plan.snapshot.user_id,
            RestoreStatus.FAILED,
            error_code="discord_api_error",
        )
        await self._store.transition_run(run_id, RunState.RESTORE_FAILED)
        raise error

    async def status_run(self, guild: Any, run_id: str) -> MigrationStatus:
        run = await self._require_run_for_guild(guild, run_id)
        members = await self._store.get_members(run_id)
        backup = build_backup(
            tuple(member.plan.snapshot for member in members),
            guild_id=int(guild.id),
            run_id=run_id,
            max_part_size=25 * 1024 * 1024,
        )
        member_counts = dict.fromkeys(MemberStatus, 0)
        restore_counts = dict.fromkeys(RestoreStatus, 0)
        latest_failure = None
        for stored_member in members:
            member_counts[stored_member.status] += 1
            restore_counts[stored_member.restore_status] += 1
            latest_failure = (
                stored_member.restore_error_code
                or stored_member.error_code
                or latest_failure
            )
        target_distribution = [0] * len(TARGET_TIER_ROLE_IDS)
        tier_by_role_id = {
            role_id: tier
            for tier, role_id in enumerate(TARGET_TIER_ROLE_IDS, start=1)
        }
        for member in guild.members:
            highest_tier = max(
                (tier_by_role_id.get(int(role.id), 0) for role in member.roles),
                default=0,
            )
            if highest_tier:
                target_distribution[highest_tier - 1] += 1
        return MigrationStatus(
            run=run,
            schema_state=await self._store.get_schema_state(int(guild.id)),
            member_counts=member_counts,
            restore_counts=restore_counts,
            target_distribution=tuple(target_distribution),
            backup_verified=backup.snapshot_sha256 == run.snapshot_sha256,
            latest_failure=latest_failure,
        )

    async def export_run(
        self, guild: Any, run_id: str, *, max_part_size: int
    ) -> BackupBundle:
        await self._require_run_for_guild(guild, run_id)
        members = await self._store.get_members(run_id)
        backup = build_backup(
            tuple(member.plan.snapshot for member in members),
            guild_id=int(guild.id),
            run_id=run_id,
            max_part_size=max_part_size,
        )
        verify_backup(backup)
        return backup

    async def finalize_run(
        self, guild: Any, run_id: str, *, completed_at: str
    ) -> CompletionReceipt:
        run = await self._require_run_for_guild(guild, run_id)
        if run.state is not RunState.VERIFIED:
            raise MigrationPreflightError(
                "invalid_migration_state", "The gate migration is not ready to finalize"
            )
        backup_artifacts = tuple(
            artifact
            for artifact in await self._store.get_artifacts(run_id)
            if artifact.kind in {"backup_part", "manifest"}
        )
        if not any(artifact.kind == "backup_part" for artifact in backup_artifacts):
            raise MigrationPreflightError(
                "missing_backup", "The gate migration backup is unavailable"
            )
        receipt = CompletionReceipt(
            run_id=run_id,
            guild_id=int(guild.id),
            completed_at=completed_at,
            snapshot_sha256=run.snapshot_sha256,
            backup_channel_id=backup_artifacts[0].channel_id,
            backup_message_ids=tuple(
                artifact.message_id for artifact in backup_artifacts
            ),
        )
        await self._store.finalize(receipt)
        return receipt

    async def _require_run_for_guild(self, guild: Any, run_id: str) -> Any:
        run = await self._store.get_run(run_id)
        if run is None or int(run.guild_id) != int(guild.id):
            raise MigrationPreflightError(
                "invalid_migration", "The gate migration does not belong to this server"
            )
        return run

    @staticmethod
    def _gate_role_ids(member: Any) -> frozenset[int]:
        return frozenset(
            int(role.id) for role in member.roles if int(role.id) in ALL_GATE_ROLE_IDS
        )

    @staticmethod
    def _tier_role_counts(guild: Any) -> tuple[int, ...]:
        counts = dict.fromkeys(TARGET_TIER_ROLE_IDS, 0)
        for member in guild.members:
            for role in member.roles:
                role_id = int(role.id)
                if role_id in counts:
                    counts[role_id] += 1
        return tuple(counts[role_id] for role_id in TARGET_TIER_ROLE_IDS)

    @staticmethod
    def _is_forbidden(error: Exception) -> bool:
        return (
            getattr(error, "status", None) == HTTP_FORBIDDEN
            or type(error).__name__ == "Forbidden"
        )

    @staticmethod
    def _plain_artifact(filename: str, data: bytes) -> BackupPart:
        return BackupPart(
            filename=filename,
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    @staticmethod
    def _preparation_pages(prepared: PreparedMigration, artifact_specs) -> tuple[str, ...]:
        summary = prepared.collection.summary
        overview = "\n".join(
            (
                f"Gate migration run: {prepared.run.run_id}",
                f"Members backed up: {summary.total_members}",
                f"Role memberships backed up: {summary.role_memberships}",
                f"Members with legacy Gate roles: {summary.legacy_members}",
                f"Members to change: {summary.changed_members}",
                f"Members unchanged: {summary.unchanged_members}",
                "Singleplayer completed after migration: "
                f"{summary.singleplayer_completed_members}",
                f"Members with duplicate SP roles: {summary.duplicate_sp_members}",
                f"Members with duplicate MP roles: {summary.duplicate_mp_members}",
                f"Members with unexpected Gate roles: {summary.unexpected_members}",
                f"Snapshot SHA-256: {prepared.backup.snapshot_sha256}",
                "All backup attachments uploaded successfully",
            )
        )
        mapping_lines = ["Target role mapping:"]
        mapping_lines.extend(
            f"Tier {tier} — {role_id}"
            for tier, role_id in enumerate(TARGET_TIER_ROLE_IDS, start=1)
        )
        mapping_lines.append(
            f"Singleplayer completed — {SINGLEPLAYER_COMPLETED_ROLE_ID}"
        )
        source_lines = ["Legacy source distribution:"]
        source_lines.extend(
            f"SP {tier} — {count}"
            for tier, count in enumerate(summary.source_sp_tiers, start=1)
        )
        source_lines.extend(
            f"MP {tier} — {count}"
            for tier, count in enumerate(summary.source_mp_tiers, start=1)
        )
        target_lines = ["Planned target distribution:"]
        target_lines.extend(
            f"Tier {tier} — {count}"
            for tier, count in enumerate(summary.target_tiers, start=1)
        )
        target_lines.extend(
            (
                f"Atomic member role updates: {summary.changed_members}",
                "Uploaded artifacts:",
                *(
                    f"{artifact.filename} — {artifact.sha256}"
                    for _, _, artifact in artifact_specs
                ),
            )
        )
        return (
            overview,
            "\n".join(mapping_lines),
            "\n".join(source_lines),
            "\n".join(target_lines),
        )

    def _validate_guild(self, guild: Any) -> None:
        if not bool(getattr(getattr(self._bot, "intents", None), "members", False)):
            raise MigrationPreflightError(
                "missing_members_intent", "The members intent is required"
            )
        bot_member = guild.me
        if bot_member is None or not bool(bot_member.guild_permissions.manage_roles):
            raise MigrationPreflightError(
                "missing_manage_roles", "Roles are configured incorrectly"
            )
        bot_role_position = int(bot_member.top_role.position)
        for role_id in ALL_GATE_ROLE_IDS:
            role = guild.get_role(role_id)
            if role is None or int(role.position) >= bot_role_position:
                raise MigrationPreflightError(
                    "invalid_gate_role", "Roles are configured incorrectly"
                )

    @staticmethod
    def _legacy_audit_csv(members: tuple[MemberMigrationPlan, ...]) -> bytes:
        output = io.StringIO(newline="")
        fieldnames = (
            "username",
            "user_id",
            "sp_role_ids",
            "mp_role_ids",
            "unexpected_role_ids",
            "selected_sp_role_id",
            "selected_mp_role_id",
        )
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for member in members:
            snapshot_role_ids = frozenset(member.snapshot.role_ids)
            sp_role_ids = tuple(
                role_id for role_id in LEGACY_SP_ROLE_IDS if role_id in snapshot_role_ids
            )
            mp_role_ids = tuple(
                role_id for role_id in LEGACY_MP_ROLE_IDS if role_id in snapshot_role_ids
            )
            writer.writerow(
                {
                    "username": member.snapshot.username,
                    "user_id": member.snapshot.user_id,
                    "sp_role_ids": " ".join(str(role_id) for role_id in sp_role_ids),
                    "mp_role_ids": " ".join(str(role_id) for role_id in mp_role_ids),
                    "unexpected_role_ids": " ".join(
                        str(role_id) for role_id in member.unexpected_role_ids
                    ),
                    "selected_sp_role_id": (
                        LEGACY_SP_ROLE_IDS[member.sp_count - 1]
                        if member.sp_count
                        else ""
                    ),
                    "selected_mp_role_id": (
                        LEGACY_MP_ROLE_IDS[member.mp_count - 1]
                        if member.mp_count
                        else ""
                    ),
                }
            )
        return output.getvalue().encode("utf-8")
