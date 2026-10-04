"""Frozen moderator-confirmed historical waves owned by JoinWatch."""

from __future__ import annotations

import asyncio
import copy
import math
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import discord

from .captcha_views import VerifyPanelView
from .joinwatch_groups import (
    PREVIEW_LIFETIME_MINUTES,
    GroupCriteria,
    historical_matches,
    utc_timestamp,
)

WAVE_BATCH_SIZE = 5
WAVE_INTERVAL_SECONDS = 10
WAVE_RETENTION_DAYS = 90
NOTIFICATION_TEXT = "Please complete the verification below"
CRITICAL_CONFIGURATION = (
    "joinwatch_auto_role_id", "joinwatch_auto_role_timer_minutes", "joinwatch_auto_role_action",
    "joinwatch_auto_role_enabled",
    "captcha_channel", "captcha_log_channel", "captcha_panel_channel_id", "captcha_panel_message_id", "dry_run",
)
ACTIVE_WAVE_STATUSES = {"running", "paused", "rolling_back"}
ALREADY_HANDLED = {"active", "pending", "verified"}
SETTLED_ENTRY_STATUSES = {"notified", "notification_unknown", "skipped", "released", "error"}


class JoinwatchWaves:
    """Freeze candidates, pace execution, and release only exact owned incidents."""

    def __init__(self, cog):
        self.cog = cog
        self._locks: dict[int, asyncio.Lock] = {}
        self._tick_locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild):
        return self._locks.setdefault(guild.id, asyncio.Lock())

    async def _configuration(self, guild):
        settings = await self.cog.config.guild(guild).all()
        return {name: settings.get(name) for name in CRITICAL_CONFIGURATION}

    async def _records(self, guild):
        return await asyncio.to_thread(self.cog._case_store.get_joinwatch_waves, guild.id)

    async def _history(self, guild):
        return await asyncio.to_thread(self.cog._case_store.get_joinwatch_history, guild.id)

    async def _save(self, guild, record):
        await asyncio.to_thread(self.cog._case_store.save_joinwatch_wave, guild.id, record)

    async def status(self, guild, wave_id):
        record = (await self._records(guild)).get(wave_id)
        if record is None:
            raise ValueError("This wave is no longer available")
        return record

    @staticmethod
    def _authorize(record, moderator_id, can_manage_messages):
        if record["moderator_id"] != moderator_id or not can_manage_messages:
            raise PermissionError("Only the moderator who opened this preview can confirm it")

    async def preview(self, guild, criteria: GroupCriteria, moderator_id, *, now=None, persist=True):
        observed = now or datetime.now(timezone.utc)
        history = await self._history(guild)
        if not history["observations"] and persist:
            raise ValueError("Import first-join history before preparing a historical wave")
        matching = await asyncio.to_thread(historical_matches, history, criteria)
        record = {
            "id": uuid.uuid4().hex, "moderator_id": moderator_id, "status": "preview",
            "criteria": asdict(criteria), "created_at": observed.isoformat(),
            "expires_at": (observed + timedelta(minutes=PREVIEW_LIFETIME_MINUTES)).isoformat(),
            "history_revision": history.get("import_revision", 0),
            "configuration": await self._configuration(guild),
            "sources": copy.deepcopy(history["sources"]),
            "source": ", ".join(item["source"] for item in history["sources"]) or "live observations",
            "complete": bool(history["sources"]) and all(item["complete"] for item in history["sources"]),
            "matching_present": 0, "already": 0, "excluded": 0, "new": 0,
            "excluded_reasons": {}, "targets": [], "entries": {},
            "notifications": {"batch_size": WAVE_BATCH_SIZE, "interval_seconds": WAVE_INTERVAL_SECONDS},
        }
        lifecycle = self.cog._joinwatch_verification
        records = await self._records(guild)
        reserved = {uid for wave in records.values() if wave["status"] in ACTIVE_WAVE_STATUSES
                    for uid in wave["targets"]}
        present = [member for uid in matching if (member := guild.get_member(uid)) is not None]
        eligibility = await lifecycle.eligibility_many(present)
        for member in present:
            uid = member.id
            record["matching_present"] += 1
            result = eligibility[uid]
            if result.status in ALREADY_HANDLED or str(uid) in reserved:
                record["already"] += 1
            elif result.status != "eligible":
                record["excluded"] += 1
                record["excluded_reasons"][str(uid)] = result.status
            else:
                record["targets"].append(str(uid))
        record["new"] = len(record["targets"])
        record["notifications"]["estimated_seconds"] = math.ceil(record["new"] / WAVE_BATCH_SIZE) * WAVE_INTERVAL_SECONDS
        if persist:
            async with self._lock(guild):
                await self._save(guild, record)
        return record

    async def validate_preview(self, guild, record, moderator_id, can_manage_messages, *, now=None):
        self._authorize(record, moderator_id, can_manage_messages)
        observed = now or datetime.now(timezone.utc)
        if utc_timestamp(record["expires_at"]) <= observed:
            raise ValueError("This preview expired. Prepare a fresh preview")
        history = await self._history(guild)
        if record["history_revision"] != history.get("import_revision", 0):
            raise ValueError("Imported history changed. Prepare a fresh preview")
        if record["configuration"] != await self._configuration(guild):
            raise ValueError("Verification settings changed. Prepare a fresh preview")

    async def confirm(self, guild, wave_id, moderator_id, can_manage_messages, *, now=None):
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            self._authorize(record, moderator_id, can_manage_messages)
            if record["status"] != "preview":
                return record
            await self.validate_preview(guild, record, moderator_id, can_manage_messages, now=now)
            await self.cog._joinwatch_verification.check_configuration(guild)
            if any(wave["status"] in ACTIVE_WAVE_STATUSES for wave in (await self._records(guild)).values()):
                raise ValueError("Another historical wave owns the queue. Finish or roll it back first")
            record["status"] = "running" if record["targets"] else "completed"
            record["confirmed_at"] = (now or datetime.now(timezone.utc)).isoformat()
            record["entries"] = {uid: {"status": "queued"} for uid in record["targets"]}
            await self._save(guild, record)
            return record

    async def cancel(self, guild, wave_id, moderator_id, can_manage_messages, *, now=None):
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            self._authorize(record, moderator_id, can_manage_messages)
            if record["status"] == "preview":
                record["status"] = "cancelled"
                await self._save(guild, record)
            return record

    async def pause(self, guild, wave_id, moderator_id, can_manage_messages, *, now=None):
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            self._authorize(record, moderator_id, can_manage_messages)
            if record["status"] == "running":
                record["status"] = "paused"
                await self._save(guild, record)
            return record

    async def finish(self, guild, wave_id, moderator_id, can_manage_messages, *, now=None):
        """Close a completed wave's controls without settling its member restrictions."""
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            self._authorize(record, moderator_id, can_manage_messages)
            if record["status"] == "finished":
                return record
            if record["status"] != "completed":
                raise ValueError("Wait for the wave to complete before marking it finished")
            record["status"] = "finished"
            record.pop("rollback", None)
            await self._save(guild, record)
            return record

    async def resume(self, guild, wave_id, moderator_id, can_manage_messages, *, now=None):
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            self._authorize(record, moderator_id, can_manage_messages)
            if record["status"] != "paused":
                return record
            if record["configuration"] != await self._configuration(guild):
                raise ValueError("Verification settings changed. Restore the confirmed configuration before resuming")
            await self.cog._joinwatch_verification.check_configuration(guild)
            await self._reconcile(guild, record)
            if any(entry["status"] in {"uncertain", "notifying", "enrolling", "releasing"} for entry in record["entries"].values()):
                await self._save(guild, record)
                raise ValueError("An interrupted effect needs moderator reconciliation before resuming")
            record["status"] = "running"
            record.pop("error", None)
            await self._save(guild, record)
            return record

    async def _reconcile(self, guild, record):
        for uid, entry in record["entries"].items():
            if entry["status"] == "notifying" or entry.get("reason") == "notification_outcome_unknown":
                entry.update(status="notification_unknown", reason="notification_outcome_unknown")
                continue
            if entry["status"] not in {"uncertain", "enrolling"}:
                continue
            member = guild.get_member(int(uid))
            active = await self.cog._joinwatch_verification.inspect(member) if member else None
            if active and active.get("incident_id") == entry.get("incident_id") and active.get("wave_id") == record["id"] and active.get("source") == "wave" and any(role.id == active["role_id"] for role in member.roles):
                entry["status"] = "enrolled"

    async def attach_message(self, guild, wave_id, channel_id, message_id):
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            record.update(channel_id=channel_id, message_id=message_id)
            await self._save(guild, record)

    async def restore(self, guild):
        async with self._lock(guild):
            records = await self._records(guild)
            for record in records.values():
                if record["status"] in {"running", "rolling_back"}:
                    record["status"] = "paused"
                for entry in record["entries"].values():
                    if entry["status"] in {"enrolling", "notifying", "releasing"}:
                        if entry["status"] == "notifying":
                            entry["reason"] = "notification_outcome_unknown"
                        entry["status"] = "uncertain"
                        record["error"] = "Interrupted effect needs moderator reconciliation"
                await self._save(guild, record)
            return list(records.values())

    async def _advance(self, guild, wave_id, uid, *, activate=True, now=None):
        lifecycle = self.cog._joinwatch_verification
        member = guild.get_member(int(uid))
        result_status = "absent" if member is None else (await lifecycle.eligibility(member)).status
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            if record["configuration"] != await self._configuration(guild):
                record.update(status="paused", error="Verification settings changed")
                await self._save(guild, record)
            if record["status"] != "running":
                return False
            entry = record["entries"][uid]
            if result_status != "eligible":
                entry.update(status="skipped", reason=result_status)
                await self._save(guild, record)
                return False
            entry["status"] = "preparing"
            await self._save(guild, record)
        prepared = await lifecycle.prepare_enrollment(member, source="wave", reasons=("group",), wave_id=wave_id)
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            if record["status"] != "running" or prepared.status == "preparing":
                return False
            entry = record["entries"][uid]
            if record["configuration"] != await self._configuration(guild):
                record.update(status="paused", error="Verification settings changed")
                await self._save(guild, record)
                return False
            if prepared.status != "ready" or not activate:
                if prepared.status != "ready":
                    record.update(status="paused", error=f"Question preparation: {prepared.status}")
                    await self._save(guild, record)
                return False
            await lifecycle.check_configuration(guild)
            # Persist before any role effect. A crash here is never silently retried.
            entry.update(status="enrolling", incident_id=prepared.incident_id)
            record["last_batch_at"] = (now or datetime.now(timezone.utc)).isoformat()
            await self._save(guild, record)
            result = await lifecycle.enroll_prepared(member, source="wave", reasons=("group",), wave_id=wave_id)
            if result.status == "enrolled":
                entry.update(status="enrolled", incident_id=result.incident_id)
            elif result.status in ALREADY_HANDLED or result.status in {"protected", "absent"}:
                entry.update(status="skipped", reason=result.status)
            else:
                entry.update(status="uncertain", reason=result.status)
                record.update(status="paused", error="Restriction outcome needs moderator reconciliation")
            await self._save(guild, record)
            return result.status == "enrolled"

    async def _notification_targets(self, guild, record):
        selected = []
        for uid, entry in record["entries"].items():
            if entry["status"] != "enrolled":
                continue
            member = guild.get_member(int(uid))
            active = await self.cog._joinwatch_verification.inspect(member) if member else None
            if not active or active["incident_id"] != entry["incident_id"] or active.get("wave_id") != record["id"]:
                entry.update(status="skipped", reason="no_longer_active_or_ready")
            elif not active.get("ready"):
                await self.cog._joinwatch_verification.prepare_enrollment(member, source="wave", reasons=("group",), wave_id=record["id"])
            elif not any(role.id == active["role_id"] for role in member.roles):
                entry.update(status="uncertain", reason="active_role_missing")
                record.update(status="paused", error="An active restriction needs moderator reconciliation")
                break
            else:
                selected.append(uid)
            if len(selected) == WAVE_BATCH_SIZE:
                break
        return selected

    async def _notify(self, guild, wave_id, *, now=None) -> bool:
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            if record["status"] != "running":
                return False
            selected = await self._notification_targets(guild, record)
            if record["status"] != "running":
                await self._save(guild, record)
                return False
            await self.cog._joinwatch_verification.check_configuration(guild)
            if not selected:
                await self._save(guild, record)
                return False
            channel = guild.get_channel(record["configuration"]["captcha_channel"])
            if channel is None:
                record.update(status="paused", error="Verification channel unavailable")
                await self._save(guild, record)
                return False
            for uid in selected:
                record["entries"][uid]["status"] = "notifying"
            record["last_batch_at"] = (now or datetime.now(timezone.utc)).isoformat()
            await self._save(guild, record)
            content = " ".join(f"<@{uid}>" for uid in selected) + "\n" + NOTIFICATION_TEXT
            kwargs = {"allowed_mentions": discord.AllowedMentions(
                everyone=False, roles=False, users=[discord.Object(id=int(uid)) for uid in selected], replied_user=False)}
            # The same persistent Verify handler handles invitations and the main panel.
            kwargs["view"] = VerifyPanelView(self.cog)
            try:
                message = await channel.send(content, delete_after=25, **kwargs)
            except Exception:
                for uid in selected:
                    record["entries"][uid].update(status="uncertain", reason="notification_outcome_unknown")
                record.update(status="paused", error="Notification outcome unknown. Do not retry the ping")
                await self._save(guild, record)
                raise
            for uid in selected:
                record["entries"][uid].update(status="notified", notification_message_id=message.id)
            await self._save(guild, record)
            return True

    async def tick(self, guild, *, now=None):
        tick_lock = self._tick_locks.setdefault(guild.id, asyncio.Lock())
        if tick_lock.locked():
            return None
        async with tick_lock:
            observed = now or datetime.now(timezone.utc)
            async with self._lock(guild):
                record = next((wave for wave in (await self._records(guild)).values() if wave["status"] == "running"), None)
                if record is None:
                    return None
                cooling_down = bool(record.get("last_batch_at")) and (observed - utc_timestamp(record["last_batch_at"])).total_seconds() < WAVE_INTERVAL_SECONDS
                if record["configuration"] != await self._configuration(guild):
                    record.update(status="paused", error="Verification settings changed")
                    await self._save(guild, record)
                    return record
                try:
                    await self.cog._joinwatch_verification.check_configuration(guild)
                except Exception:
                    record.update(status="paused", error="Verification configuration is unavailable")
                    await self._save(guild, record)
                    raise
                queued = [] if any(entry["status"] == "enrolled" for entry in record["entries"].values()) else [uid for uid, entry in record["entries"].items() if entry["status"] in {"queued", "preparing"}][:WAVE_BATCH_SIZE]
            try:
                activated = False
                for uid in queued:
                    activated = await self._advance(guild, record["id"], uid, activate=not cooling_down, now=now) or activated
                notified = await self._notify(guild, record["id"], now=now) if not cooling_down else False
            except Exception:
                async with self._lock(guild):
                    failed = await self.status(guild, record["id"])
                    failed.update(status="paused", error="Wave execution failed. Check moderator diagnostics")
                    await self._save(guild, failed)
                raise
            async with self._lock(guild):
                record = await self.status(guild, record["id"])
                # Preparation uses the pause between batches without consuming it.
                if activated or notified:
                    record["last_batch_at"] = (now or datetime.now(timezone.utc)).isoformat()
                if record["status"] == "running" and all(entry["status"] in SETTLED_ENTRY_STATUSES for entry in record["entries"].values()):
                    record.update(status="completed", completed_at=observed.isoformat())
                await self._save(guild, record)
                return record

    async def rollback_preview(self, guild, wave_id, moderator_id, can_manage_messages, *, now=None):
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            self._authorize(record, moderator_id, can_manage_messages)
            if record["status"] == "finished":
                raise ValueError("This wave is finished and can no longer be rolled back")
            targets = []
            for uid, entry in record["entries"].items():
                active = await self.cog._joinwatch_verification.inspect_id(guild, int(uid))
                if active and active.get("wave_id") == wave_id and active["incident_id"] == entry.get("incident_id"):
                    targets.append({"user_id": uid, "incident_id": active["incident_id"]})
            token = uuid.uuid4().hex
            record["rollback"] = {"token": token, "targets": targets,
                                  "expires_at": ((now or datetime.now(timezone.utc)) + timedelta(minutes=PREVIEW_LIFETIME_MINUTES)).isoformat()}
            if record["status"] == "running":
                record["status"] = "paused"
            await self._save(guild, record)
            return {"id": wave_id, "rollback_count": len(targets), "confirmation_token": token}

    async def rollback(self, guild, wave_id, moderator_id, can_manage_messages, confirmation_token, *, now=None):
        async with self._lock(guild):
            record = await self.status(guild, wave_id)
            self._authorize(record, moderator_id, can_manage_messages)
            if record["status"] == "finished":
                raise ValueError("This wave is finished and can no longer be rolled back")
            rollback = record.get("rollback")
            if not rollback or rollback["token"] != confirmation_token or utc_timestamp(rollback["expires_at"]) <= (now or datetime.now(timezone.utc)):
                raise ValueError("Rollback confirmation expired or changed")
            record["status"] = "rolling_back"
            await self._save(guild, record)
            await self.cog._joinwatch_verification.cancel_wave_preparation(guild, wave_id)
            for target in rollback["targets"]:
                uid = target["user_id"]
                active = await self.cog._joinwatch_verification.inspect_id(guild, int(uid))
                if not active or active.get("wave_id") != wave_id or active["incident_id"] != target["incident_id"]:
                    continue
                record["entries"][uid]["status"] = "releasing"
                await self._save(guild, record)
                try:
                    result = await self.cog._joinwatch_verification.release_id(guild, int(uid), incident_id=target["incident_id"], outcome="rollback")
                except Exception:
                    record.update(status="paused", error="Rollback outcome needs moderator reconciliation")
                    record["entries"][uid]["status"] = "uncertain"
                    await self._save(guild, record)
                    raise
                if result.status not in {"released", "retained", "complete", "complete_restricted", "completed", "verified", "stale"}:
                    record["entries"][uid].update(status="uncertain", reason=result.status)
                    record.update(status="paused", error="Rollback release needs moderator reconciliation")
                    await self._save(guild, record)
                    return record
                record["entries"][uid]["status"] = "released"
                await self._save(guild, record)
            record.update(status="rolled_back", completed_at=(now or datetime.now(timezone.utc)).isoformat())
            record.pop("rollback", None)
            await self._save(guild, record)
            return record

    async def delete_user(self, guild, user_id):
        async with self._lock(guild):
            records = await self._records(guild)
            uid = str(user_id)
            for record in records.values():
                if record["moderator_id"] == user_id:
                    record["moderator_id"] = 0
                    if record["status"] == "running":
                        record.update(status="paused", error="Wave moderator data was deleted")
                record["targets"] = [target for target in record["targets"] if target != uid]
                record["entries"].pop(uid, None)
                record["excluded_reasons"].pop(uid, None)
                if "rollback" in record:
                    record["rollback"]["targets"] = [target for target in record["rollback"]["targets"] if target["user_id"] != uid]
                await self._save(guild, record)

    async def delete_guild(self, guild):
        async with self._lock(guild):
            await asyncio.to_thread(self.cog._case_store.clear_joinwatch_auxiliary, guild.id)

    async def prune(self, guild, *, now=None):
        observed = now or datetime.now(timezone.utc)
        cutoff = observed - timedelta(days=WAVE_RETENTION_DAYS)
        async with self._lock(guild):
            records = await self._records(guild)
            active_roles = await self.cog.config.guild(guild).joinwatch_pending_roles()
            owned_waves = {entry.get("wave_id") for entry in active_roles.values()}
            stale = [key for key, record in records.items()
                     if key not in owned_waves and ((record["status"] in {"preview", "cancelled"} and utc_timestamp(record["expires_at"]) < observed)
                     or (record["status"] in {"completed", "finished", "rolled_back"} and utc_timestamp(record.get("completed_at", record["created_at"])) < cutoff))]
            for key in stale:
                await asyncio.to_thread(self.cog._case_store.delete_joinwatch_wave, guild.id, key)
            return len(stale)
