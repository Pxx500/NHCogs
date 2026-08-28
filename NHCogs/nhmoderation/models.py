from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ModerationObservation:
    guild_id: int
    source_kind: str
    source_key: str | None
    action_hint: str
    target_user_id: int | None
    observed_at: datetime
    executor_user_id: int | None = None
    credited_moderator_hint: int | None = None
    attribution_hint: str | None = None
    occurred_at: datetime | None = None
    reason: str | None = None
    expiry_at: datetime | None = None
    channel_id: int | None = None
    import_batch_id: str | None = None
    source_payload_version: int = 1


@dataclass(frozen=True)
class StoredObservation(ModerationObservation):
    observation_id: int = 0


@dataclass(frozen=True)
class ProjectedAction:
    action_kind: str
    action_variant: str | None
    target_user_id: int | None
    moderator_user_id: int | None
    attribution_kind: str
    attribution_confidence: str
    occurred_at: datetime | None
    lifecycle_at: datetime
    expiry_at: datetime | None
    ended_at: datetime | None
    reason: str | None
    current_state: str | None
    observation_ids: tuple[int, ...]


@dataclass(frozen=True)
class BanChartQuery:
    guild_id: int
    since: datetime | None = None
    amount: int = 10
    include_automation: bool = False


@dataclass(frozen=True)
class BanChartRow:
    moderator_user_id: int | None
    label: str | None
    count: int


@dataclass(frozen=True)
class BanChartData:
    rows: tuple[BanChartRow, ...]
    other_count: int
    total_count: int


@dataclass(frozen=True)
class SynchronizationState:
    guild_id: int
    audit_ban_cursor: int | None
    audit_unban_cursor: int | None
    red_modlog_cursor: int | None
    last_sync_at: datetime | None
    last_reconciliation_at: datetime | None
    historical_gap: bool
    migration_state: str
    projection_checkpoint: int
    projection_version: int


@dataclass(frozen=True)
class MigrationRun:
    run_id: str
    guild_id: int
    state: str
    started_at: datetime
    completed_at: datetime | None
    completed_steps: frozenset[str]
    report: str | None
