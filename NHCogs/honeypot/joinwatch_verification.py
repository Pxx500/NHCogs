"""JoinWatch-owned CAPTCHA enrollment, attempts, and exact-incident release."""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from NHCogs.account_snapshot import account_snapshot

from . import joinwatch_publication, joinwatch_state
from .captcha import CaptchaPreparation, CaptchaQuestion, generate_challenge

MAX_ATTEMPTS = 2
PREPARED_ENROLLMENT_CAPACITY = 32
log = logging.getLogger("red.Honeypot")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: str
    question: CaptchaQuestion | None = None
    session_id: str | None = None
    stage: int = 0
    deadline: datetime | None = None
    attempts_remaining: int = 2
    incident_id: str | None = None


class JoinwatchVerification:
    """Keep eligibility in the existing active-penalty register, not in sessions."""

    def __init__(self, cog):
        self.cog = cog
        self.preparation = CaptchaPreparation()
        self._planned: dict[tuple[int, int], dict] = {}
        self._fill_tasks: set[asyncio.Task] = set()
        self._deleted_challenges: set[tuple] = set()
        self._group_locks: dict[int, asyncio.Lock] = {}
        self._cancelled_waves: set[tuple[int, str]] = set()

    async def capture_enrollment(self, member, entry):
        """Capture available context before restriction without fetching Discord data."""
        if entry.get("history"):
            return
        now = datetime.now(timezone.utc)
        entry.setdefault("incident_id", secrets.token_hex(16))
        profile = account_snapshot(member)
        activity_owner = self.cog.bot.get_cog("NHMisc")
        activity = {"availability": "unavailable", "messages": None, "active_days": None,
                    "distinct_channels": None, "captured_at": now.isoformat(),
                    "window_start": None, "window_end": None, "coverage": None}
        if activity_owner is not None:
            try:
                activity = await activity_owner.get_member_activity_summary(member.guild.id, member.id, now=now)
            except Exception:
                log.exception("Could not capture JoinWatch activity context")
        first_join = None
        try:
            first_join = await asyncio.to_thread(
                self.cog._case_store.get_joinwatch_observation, member.guild.id, member.id
            )
        except Exception:
            log.exception("Could not capture retained first-join context")
        entry["history"] = {
            "captured_at": now.isoformat(), "history_origin": "enrollment",
            "profile": profile, "activity": activity,
            "first_join": ({key: first_join.get(key) for key in ("first_joined_at", "imported")}
                           if first_join is not None else None),
            "enrolled_at": now.isoformat(), "original_deadline": entry.get("expires_at"),
        }

    @staticmethod
    def event(entry, kind, *, now=None):
        entry.setdefault("incident_id", secrets.token_hex(16))
        entry.setdefault("history_events", []).append({
            "event_id": secrets.token_hex(16), "incident_id": entry["incident_id"],
            "kind": kind, "occurred_at": (now or datetime.now(timezone.utc)).isoformat(),
            "failures": entry.get("failures", 0), "stage": entry.get("stage", 0),
        })

    async def _archive(self, guild, user_id, entry):
        entry.setdefault("incident_id", secrets.token_hex(16))
        entry.setdefault("history", {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "history_origin": "adopted_active_state", "profile": None, "activity": None,
            "first_join": None, "enrolled_at": entry.get("applied_at"),
            "original_deadline": entry.get("expires_at"),
        })
        record = {**entry["history"], "incident_id": entry["incident_id"],
                  "guild_id": str(guild.id), "user_id": str(user_id),
                  "outcome": entry.get("history_terminal", "pending")}
        for key in ("source", "wave_id", "reasons", "failures", "stage", "captcha_enabled",
                    "applied_at", "expires_at", "completed_at", "independent_restriction",
                    "completion_reason", "modlog_case_number", "punishment_started_at", "pre_action"):
            record[key] = entry.get(key)
        record["test"] = bool(entry.get("test"))
        for key in ("role_id", "enrollment_moderator", "completion_moderator"):
            record[key] = str(entry[key]) if entry.get(key) is not None else None
        try:
            await asyncio.to_thread(self.cog._case_store.save_verification_history,
                                    record, entry.get("history_events", []))
        except Exception as error:
            log.exception("Could not settle JoinWatch history for guild %s", guild.id)
            try:
                await self.cog._record_operational_failure(
                    guild.id, "joinwatch_history", f"Verification history settlement failed: {error}")
            except Exception:
                log.exception("Could not report JoinWatch history storage failure")
            return False
        entry.pop("history_events", None)
        return True

    async def persist_history(self, guild, user_id, entry, *, store_name="joinwatch_pending_roles"):
        """Save retryable history with the existing incident, then settle off-thread."""
        entry.setdefault("incident_id", secrets.token_hex(16))
        config_store = getattr(self.cog.config.guild(guild), store_name)
        async with config_store() as entries:
            entries[str(user_id)] = dict(entry)
        settled = await self._archive(guild, user_id, entry)
        async with config_store() as entries:
            if entries.get(str(user_id), {}).get("incident_id") == entry["incident_id"]:
                entries[str(user_id)] = dict(entry)
        return settled

    async def finish(self, guild, user_id, entry, outcome, *, store_name="joinwatch_pending_roles", retain_pending=False):
        """Remember a terminal fact before cleanup. Never repeat a Discord effect for logging."""
        entry.setdefault("incident_id", secrets.token_hex(16))
        if not entry.get("history_terminal"):
            entry["history_terminal"] = outcome
            entry["completed_at"] = datetime.now(timezone.utc).isoformat()
            self.event(entry, "completed")
        if not await self.persist_history(guild, user_id, entry, store_name=store_name):
            return False
        if retain_pending:
            return True
        async with getattr(self.cog.config.guild(guild), store_name)() as entries:
            if entries.get(str(user_id), {}).get("incident_id") == entry["incident_id"]:
                entries.pop(str(user_id), None)
        return True

    async def export_history(self, guild_id):
        return await asyncio.to_thread(self.cog._case_store.export_verification_history, guild_id)

    async def _infrastructure_failure(self, member, entry, kind):
        key = f"{kind}:{entry.get('failures', 0)}:{entry.get('stage', 0)}"
        errors = entry.setdefault("history_errors", [])
        if key not in errors:
            errors.append(key)
            self.event(entry, kind)
            await self._save(member, entry)

    async def _settings(self, guild) -> dict:
        config = self.cog.config.guild(guild)
        names = (
            "dry_run",
            "joinwatch_auto_role_id",
            "joinwatch_auto_role_timer_minutes",
            "joinwatch_auto_role_random_delay_enabled",
            "joinwatch_auto_role_random_delay_min_minutes",
            "joinwatch_auto_role_random_delay_max_minutes",
            "joinwatch_groups_enabled",
            "joinwatch_groups_max_active",
            "joinwatch_groups_per_minute",
            "joinwatch_group_admission_times",
            "joinwatch_pending_roles",
            "joinwatch_pending_role_assignments",
            "joinwatch_verified_members",
            "captcha_channel",
            "captcha_log_channel",
            "captcha_panel_channel_id",
            "captcha_panel_message_id",
        )
        values = await asyncio.gather(
            *(config.get_raw(name) for name in names)
        )
        return dict(zip(names, values, strict=True))

    async def _entry(self, member) -> dict | None:
        entry = await self.cog.config.guild(member.guild).get_raw(
            "joinwatch_pending_roles", str(member.id), default=None
        )
        return dict(entry) if isinstance(entry, dict) else None

    async def _save(self, member, entry: dict, *, expected: str | None = None) -> bool:
        async with self.cog.config.guild(member.guild).joinwatch_pending_roles() as entries:
            current = entries.get(str(member.id))
            if expected is not None and (current is None or current.get("incident_id") != expected):
                return False
            entries[str(member.id)] = dict(entry)
        await self.persist_history(member.guild, member.id, entry)
        return True

    @staticmethod
    def _key(member, entry: dict) -> tuple:
        return (member.guild.id, member.id, entry["incident_id"], entry.get("failures", 0))

    @staticmethod
    def _result(status: str, entry: dict | None = None, **kwargs) -> VerificationResult:
        entry = entry or {}
        deadline = None
        if entry.get("expires_at"):
            deadline = datetime.fromisoformat(entry["expires_at"])
        return VerificationResult(
            status,
            deadline=deadline,
            attempts_remaining=max(0, 2 - entry.get("failures", 0)),
            incident_id=entry.get("incident_id"),
            **kwargs,
        )

    async def eligibility(self, member) -> VerificationResult:
        return await self._eligibility(member, await self._settings(member.guild))

    async def eligibility_many(self, members) -> dict[int, VerificationResult]:
        members = tuple(members)
        if not members:
            return {}
        settings = await self._settings(members[0].guild)
        return {member.id: await self._eligibility(member, settings) for member in members}

    async def _eligibility(self, member, settings) -> VerificationResult:  # noqa: PLR0911 - explicit enrollment refusal outcomes
        if member.bot or await self.cog._is_protected_member(member):
            return VerificationResult("protected")
        entry = settings.get("joinwatch_pending_roles", {}).get(str(member.id))
        if entry:
            return self._result("active", entry)
        if str(member.id) in settings.get("joinwatch_pending_role_assignments", {}):
            return VerificationResult("pending")
        role = member.guild.get_role(settings.get("joinwatch_auto_role_id"))
        if role is None:
            return VerificationResult("unavailable")
        if role in member.roles:
            return VerificationResult("ambiguous")
        if str(member.id) in settings.get("joinwatch_verified_members", {}):
            return VerificationResult("verified")
        if self.cog._missing_role_assignment_permission(member.guild, role):
            return VerificationResult("unavailable")
        if settings.get("dry_run"):
            return VerificationResult("dry_run")
        return VerificationResult("eligible")

    async def prepare_enrollment(self, member, *, source="wave", reasons=(), wave_id=None):  # noqa: PLR0911 - cancellation and enrollment refusal outcomes
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
            if source == "wave" and (member.guild.id, wave_id) in self._cancelled_waves:
                return VerificationResult("unavailable")
            allowed = await self.eligibility(member)
            if allowed.status == "active":
                active = await self._entry(member)
                if active and active.get("challenge") and active.get("incident_id"):
                    self.preparation.request(
                        self._key(member, active), active["challenge"], priority=0
                    )
            if allowed.status not in ("eligible", "verified") or (
                allowed.status == "verified" and source != "test"
            ):
                return allowed
            key = (member.guild.id, member.id)
            entry = self._planned.get(key)
            if entry is None:
                if len(self._planned) >= PREPARED_ENROLLMENT_CAPACITY:
                    return VerificationResult("preparing")
                settings = await self._settings(member.guild)
                if source == "wave" and (member.guild.id, wave_id) in self._cancelled_waves:
                    return VerificationResult("unavailable")
                entry = {
                    "incident_id": secrets.token_hex(16),
                    "source": source,
                    "reasons": list(reasons),
                    "wave_id": wave_id,
                    "test": source == "test",
                    "captcha_enabled": True,
                    "role_id": settings["joinwatch_auto_role_id"],
                    "failures": 0,
                    "challenge": [generate_challenge(), generate_challenge()],
                    "stage": 0,
                }
                self._planned[key] = entry
            elif entry["source"] != source or entry.get("wave_id") != wave_id:
                return self._result("active", entry)
            preparation_key = self._key(member, entry)
            ready = self.preparation.get(preparation_key)
            if ready is None:
                if self.preparation.failed(preparation_key):
                    await self._audit(member, entry, "Image preparation failed")
                    return self._result("error", entry)
                self.preparation.request(preparation_key, entry["challenge"])
            return self._result("ready" if ready else "preparing", entry)

    async def cancel_wave_preparation(self, guild, wave_id) -> int:
        """Retire this wave's unactivated work without changing any active penalty."""
        self._cancelled_waves.add((guild.id, wave_id))
        pending = await self.cog.config.guild(guild).get_raw("joinwatch_pending_role_assignments", default={})
        keys = {key for key, entry in self._planned.items()
                if key[0] == guild.id and entry.get("source") == "wave" and entry.get("wave_id") == wave_id}
        keys.update((guild.id, int(user_id)) for user_id, entry in pending.items()
                    if entry.get("source") == "wave" and entry.get("wave_id") == wave_id)
        cancelled = 0
        for key in keys:
            async with joinwatch_state.member_lock(self.cog, guild.id, key[1]):
                discarded = []
                entry = self._planned.get(key)
                if entry is not None and entry.get("source") == "wave" and entry.get("wave_id") == wave_id:
                    discarded.append(self._planned.pop(key))
                cancel_assignment = False
                async with self.cog.config.guild(guild).joinwatch_pending_role_assignments() as assignments:
                    entry = assignments.get(str(key[1]))
                    if entry is not None and entry.get("source") == "wave" and entry.get("wave_id") == wave_id:
                        discarded.append(dict(entry))
                        cancel_assignment = True
                if cancel_assignment:
                    await joinwatch_state.delete_pending_assignment(self.cog, guild, key[1])
                for entry in discarded:
                    preparation_key = (guild.id, key[1], entry["incident_id"], entry.get("failures", 0))
                    self._deleted_challenges.add(preparation_key)
                    self.preparation.forget(preparation_key)
                cancelled += bool(discarded)
        return cancelled

    async def enroll_prepared(self, member, *, source="wave", reasons=(), wave_id=None, moderator_id=None):  # noqa: PLR0911 - enrollment validation precedes effects
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
            if source == "wave" and (member.guild.id, wave_id) in self._cancelled_waves:
                return VerificationResult("unavailable")
            allowed = await self.eligibility(member)
            if allowed.status not in ("eligible", "verified") or (
                allowed.status == "verified" and source != "test"
            ):
                return allowed
            key = (member.guild.id, member.id)
            entry = self._planned.get(key)
            if entry is None or entry["source"] != source or entry.get("wave_id") != wave_id:
                return VerificationResult("unavailable")
            if self.preparation.get(self._key(member, entry)) is None:
                return self._result("preparing", entry)
            settings = await self._settings(member.guild)
            if settings.get("joinwatch_auto_role_id") != entry["role_id"]:
                self._planned.pop(key, None)
                return VerificationResult("unavailable")
            if not await self.cog._punitive_effect_allowed(member.guild):
                return VerificationResult("dry_run")
            now = datetime.now(timezone.utc)
            entry = dict(entry)
            if moderator_id is not None:
                entry["enrollment_moderator"] = moderator_id
            entry.setdefault("expires_at", (now + timedelta(minutes=settings.get("joinwatch_auto_role_timer_minutes", 1440))).isoformat())
            entry.update(
                {
                    "verification_state": "enrolling",
                    "member_id": member.id,
                }
            )
            await self.capture_enrollment(member, entry)
            self._planned[key] = entry
            await joinwatch_state.store_pending_assignment(
                self.cog, member, entry["role_id"], now,
                expires_at=datetime.fromisoformat(entry["expires_at"]), incident=entry,
            )
            role = member.guild.get_role(entry["role_id"])
            try:
                await member.add_roles(role, reason="Automated account status update.")
            except discord.HTTPException as error:
                if getattr(error, "status", None) in (400, 403, 404):
                    await joinwatch_state.delete_pending_assignment(self.cog, member.guild, member.id, outcome="action_failed")
                    await self._audit(member, entry, "Restriction application failed")
                else:
                    await self._audit(member, entry, "Restriction outcome needs moderator review")
                return self._result("error", entry)
            entry.update(
                {
                    "verification_state": "active",
                    "applied_at": now.isoformat(),
                    "effect_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self.event(entry, "enrolled")
            await self._save(member, entry)
            await joinwatch_state.delete_pending_assignment(self.cog, member.guild, member.id)
            self._planned.pop(key, None)
            await self._record_enrollment(member.guild, entry)
            await self._audit(member, entry, "Awaiting verification")
            return self._result("enrolled", entry)

    async def enroll_test(self, member, *, moderator_id=None):
        existing = await self._entry(member)
        if existing and existing.get("test") and existing.get("failures", 0) >= MAX_ATTEMPTS:
            released = await self.release(
                member,
                incident_id=existing.get("incident_id"),
                outcome="test_reset",
                moderator_id=moderator_id,
            )
            if released.status != "complete":
                return released
        result = await self.prepare_enrollment(member, source="test", reasons=("test",))
        if result.status == "preparing":
            await self.preparation.wait()
        elif result.status != "ready":
            return result
        return await self.enroll_prepared(member, source="test", reasons=("test",), moderator_id=moderator_id)

    async def prepare_assignment(self, member, incident: dict) -> None:
        """Prepare during the existing delay without changing either timestamp."""
        if not incident.get("incident_id"):
            incident["incident_id"] = secrets.token_hex(16)
        incident["captcha_enabled"] = True
        incident.setdefault("source", "join")
        incident.setdefault("reasons", ["age"])
        incident.setdefault("failures", 0)
        incident.setdefault("stage", 0)
        await self.capture_enrollment(member, incident)
        if "challenge" not in incident:
            incident["challenge"] = [generate_challenge(), generate_challenge()]
        self.preparation.request(self._key(member, incident), incident["challenge"])

    async def schedule_group(self, member):
        """Use JoinWatch's existing delayed-assignment executor for live cohorts."""
        async with self._group_locks.setdefault(member.guild.id, asyncio.Lock()):
            return await self._schedule_group_locked(member)

    async def _schedule_group_locked(self, member):
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
            settings = await self._settings(member.guild)
            if not settings.get("joinwatch_groups_enabled"):
                return VerificationResult("unavailable")
            allowed = await self.eligibility(member)
            if allowed.status not in ("eligible", "active", "pending"):
                return allowed
            existing = settings.get("joinwatch_pending_roles", {}).get(str(member.id))
            assignment = settings.get("joinwatch_pending_role_assignments", {}).get(str(member.id))
            if existing or assignment:
                entry = dict(existing or assignment)
                reasons = set(entry.get("reasons", []))
                reasons.add("group")
                entry["reasons"] = sorted(reasons)
                await self.prepare_assignment(member, entry)
                if existing:
                    await self._save(member, entry)
                else:
                    async with self.cog.config.guild(
                        member.guild
                    ).joinwatch_pending_role_assignments() as entries:
                        entries[str(member.id)] = entry
                return self._result("active" if existing else "pending", entry)
            if not await self._group_configuration_ready(member.guild):
                return VerificationResult("unavailable")
            now = datetime.now(timezone.utc)
            reserved = sum(
                entry.get("source") == "group" and not entry.get("test")
                for store_name in ("joinwatch_pending_roles", "joinwatch_pending_role_assignments")
                for entry in settings.get(store_name, {}).values()
            )
            timestamps = [
                stamp
                for stamp in settings.get("joinwatch_group_admission_times", [])
                if isinstance(stamp, (int, float)) and stamp > now.timestamp() - 60
            ]
            if reserved >= (settings.get("joinwatch_groups_max_active") or 50) or len(
                timestamps
            ) >= (settings.get("joinwatch_groups_per_minute") or 5):
                await self.cog._record_operational_failure(
                    member.guild.id,
                    "joinwatch_group_capacity",
                    "Live group enrollment paused at the configured active or per-minute limit",
                )
                return VerificationResult("capacity")
            expires_at = now + timedelta(
                minutes=settings.get("joinwatch_auto_role_timer_minutes", 1440)
            )
            delay = 0
            if settings.get("joinwatch_auto_role_random_delay_enabled"):
                minimum = max(1, settings.get("joinwatch_auto_role_random_delay_min_minutes", 1))
                maximum = max(
                    minimum, settings.get("joinwatch_auto_role_random_delay_max_minutes", 10)
                )
                delay = random.randint(minimum, maximum)
            incident = joinwatch_state.build_incident(
                member,
                now=now,
                expires_at=expires_at,
                account_age_hours=max(1, int((now - member.created_at).total_seconds() / 3600)),
            )
            incident.update({"source": "group", "reasons": ["group"]})
            await self.prepare_assignment(member, incident)
            await joinwatch_state.store_pending_assignment(
                self.cog,
                member,
                settings["joinwatch_auto_role_id"],
                now + timedelta(minutes=delay),
                expires_at=expires_at,
                incident=incident,
            )
            timestamps.append(now.timestamp())
            await self.cog.config.guild(member.guild).joinwatch_group_admission_times.set(
                timestamps
            )
            return self._result("scheduled", incident)

    async def _group_configuration_ready(self, guild) -> bool:
        try:
            await self.check_configuration(guild)
        except ValueError:
            await self.cog._record_operational_failure(
                guild.id, "joinwatch_group_configuration",
                "Live group enrollment stopped because CAPTCHA configuration is unavailable",
            )
            return False
        return True

    async def check_scheduled_group_assignment(self, guild, user_id, entry) -> bool:
        """Discard an unfulfilled group-only assignment if its verification route is gone."""
        if (entry.get("test") or entry.get("applied_at")
                or entry.get("restore_incident") or "age" in entry.get("reasons", [])):
            return True
        if await self._group_configuration_ready(guild):
            return True
        await joinwatch_state.delete_pending_assignment(self.cog, guild, user_id)
        if entry.get("incident_id"):
            self.preparation.forget((guild.id, user_id, entry["incident_id"], entry.get("failures", 0)))
        return False

    async def start(self, member, *, now=None) -> VerificationResult:  # noqa: PLR0911 - active incident guards precede session exposure
        now = now or datetime.now(timezone.utc)
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
            entry = await self._entry(member)
            if entry is not None and entry.get("history_terminal"):
                return self._result("complete_restricted" if entry.get("independent_restriction") else "complete", entry)
            if entry is None or not entry.get("captcha_enabled"):
                return VerificationResult("unavailable")
            if member.bot or await self.cog._is_protected_member(member):
                return self._result("protected", entry)
            if entry.get("verification_state") == "release_pending":
                return await self._release_locked(member, entry)
            if not entry.get("test") and datetime.fromisoformat(entry["expires_at"]) <= now:
                return self._result("unavailable", entry)
            if entry.get("failures", 0) >= MAX_ATTEMPTS:
                return self._result("locked", entry)
            entry.setdefault("incident_id", secrets.token_hex(16))
            entry.setdefault("stage", 0)
            if "challenge" not in entry:
                entry["challenge"] = [generate_challenge(), generate_challenge()]
            key = self._key(member, entry)
            questions = self.preparation.get(key)
            await self._save(member, entry)
            if questions is None:
                if self.preparation.failed(key):
                    await self._infrastructure_failure(member, entry, "preparation_failed")
                    await self._audit(member, entry, "Image preparation failed")
                    return self._result("error", entry)
                self.preparation.request(key, entry["challenge"], priority=0)
                return self._result("preparing", entry)
            session_expiry = entry.get("session_expires_at")
            if (
                not entry.get("session_id")
                or not session_expiry
                or datetime.fromisoformat(session_expiry) <= now
            ):
                entry["session_id"] = secrets.token_hex(16)
                entry["session_expires_at"] = (now + timedelta(minutes=5)).isoformat()
                self.event(entry, "started", now=now)
                await self._save(member, entry, expected=entry["incident_id"])
            return self._result(
                "question",
                entry,
                question=questions[entry["stage"]],
                session_id=entry["session_id"],
                stage=entry["stage"],
            )

    async def submit(self, member, session_id, stage, choice, *, now=None) -> VerificationResult:  # noqa: PLR0911 - atomic validation and attempt outcomes
        now = now or datetime.now(timezone.utc)
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
            entry = await self._entry(member)
            if (
                entry is None
                or not entry.get("captcha_enabled")
                or entry.get("session_id") != session_id
                or entry.get("stage", 0) != stage
            ):
                return VerificationResult("stale")
            if entry.get("verification_state") == "release_pending":
                return await self._release_locked(member, entry)
            if entry.get("failures", 0) >= MAX_ATTEMPTS:
                return self._result("locked", entry)
            if (
                not entry.get("session_expires_at")
                or datetime.fromisoformat(entry["session_expires_at"]) <= now
                or (not entry.get("test") and datetime.fromisoformat(entry["expires_at"]) <= now)
            ):
                return self._result("stale", entry)
            if member.bot or await self.cog._is_protected_member(member):
                return self._result("protected", entry)
            if not await self.cog._punitive_effect_allowed(member.guild):
                return self._result("dry_run", entry)
            if choice not in range(6) or isinstance(choice, bool):
                return self._result("stale", entry)
            questions = self.preparation.get(self._key(member, entry))
            if questions is None:
                self.preparation.request(self._key(member, entry), entry["challenge"], priority=0)
                return self._result("preparing", entry)
            if choice != entry["challenge"][stage]["answer"]:
                old_key = self._key(member, entry)
                entry["failures"] = entry.get("failures", 0) + 1
                entry["stage"] = 0
                entry.pop("session_id", None)
                entry.pop("session_expires_at", None)
                entry.pop("challenge", None)
                if entry["failures"] < MAX_ATTEMPTS:
                    entry["challenge"] = [generate_challenge(), generate_challenge()]
                self.event(entry, "locked" if entry["failures"] == MAX_ATTEMPTS else "incorrect", now=now)
                await self._save(member, entry, expected=entry["incident_id"])
                self.preparation.forget(old_key)
                if entry["failures"] < MAX_ATTEMPTS:
                    self.preparation.request(
                        self._key(member, entry), entry["challenge"], priority=0
                    )
                return self._result(
                    "locked" if entry["failures"] == MAX_ATTEMPTS else "incorrect", entry
                )
            if stage == 0:
                entry["stage"] = 1
                self.event(entry, "stage_passed", now=now)
                await self._save(member, entry, expected=entry["incident_id"])
                return self._result(
                    "question", entry, question=questions[1], session_id=session_id, stage=1
                )
            entry["verification_state"] = "release_pending"
            entry["completion_outcome"] = "passed"
            self.event(entry, "passed", now=now)
            await self._save(member, entry, expected=entry["incident_id"])
            return await self._release_locked(member, entry)

    async def release(
        self, member, *, incident_id=None, outcome="manual", moderator_id=None, reason=None
    ):
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
            entry = await self._entry(member)
            if entry is None or (
                incident_id is not None and entry.get("incident_id") != incident_id
            ):
                return VerificationResult("stale")
            if not await self.cog._punitive_effect_allowed(member.guild):
                return self._result("dry_run", entry)
            entry.setdefault("incident_id", secrets.token_hex(16))
            entry.update(
                {
                    "verification_state": "release_pending",
                    "completion_outcome": outcome,
                    "completion_moderator": moderator_id,
                    "completion_reason": reason,
                }
            )
            await self._save(member, entry)
            return await self._release_locked(member, entry)

    async def _release_locked(self, member, entry):
        if not await self.cog._punitive_effect_allowed(member.guild):
            return self._result("dry_run", entry)
        role = member.guild.get_role(entry["role_id"])
        if (entry["role_id"] not in entry.get("manual_role_reasons", [])
                and role is not None and role in member.roles):
            store = getattr(self.cog, "_case_store", None)
            if store is not None:
                await asyncio.to_thread(store.transfer_joinwatch_role_to_pending_case,
                    member.guild.id, member.id, entry["role_id"], datetime.now(timezone.utc))
        retained = await self._independent_role_reason(member, entry["role_id"])
        if role is not None and role in member.roles and not retained:
            if self.cog._missing_role_assignment_permission(member.guild, role):
                await self._infrastructure_failure(member, entry, "release_failed")
                return self._result("release_pending", entry)
            try:
                await member.remove_roles(role, reason="Account verification completed.")
            except discord.HTTPException:
                await self._infrastructure_failure(member, entry, "release_failed")
                await self._audit(member, entry, "Restriction release failed")
                return self._result("release_pending", entry)
        if not entry.get("test") and entry.get("completion_outcome") == "passed":
            async with self.cog.config.guild(member.guild).joinwatch_verified_members() as verified:
                verified[str(member.id)] = {
                    "incident_id": entry["incident_id"],
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
        entry["independent_restriction"] = retained
        await joinwatch_state.delete_pending_assignment(self.cog, member.guild, member.id)
        await self.finish(member.guild, member.id, entry, entry.get("completion_outcome", "manual"))
        self.preparation.forget(self._key(member, entry))
        await self._audit(
            member,
            entry,
            f"Completed ({entry.get('completion_outcome', 'manual')})"
            + (". An independent restriction remains" if retained else ""),
            role_status=f"<@&{entry['role_id']}> retained" if retained else "Removed",
        )
        return self._result("complete_restricted" if retained else "complete", entry)

    async def _independent_role_reason(self, member, role_id):
        entry = await self._entry(member)
        if entry and role_id in entry.get("manual_role_reasons", []):
            return True
        store = getattr(self.cog, "_case_store", None)
        if store is None:
            return False
        return await asyncio.to_thread(
            store.role_required_by_case, member.guild.id, member.id, role_id
        )

    async def _record_enrollment(self, guild, entry):
        if entry.get("source") == "wave" and not entry.get("test"):
            await asyncio.to_thread(
                self.cog._case_store.record_wave_enrollment,
                guild.id,
                datetime.fromisoformat(entry["effect_at"]),
                entry["incident_id"],
            )

    async def inspect(self, member) -> dict | None:
        return await self.inspect_id(member.guild, member.id)

    async def inspect_id(self, guild, user_id) -> dict | None:
        entry = (await self._settings(guild)).get("joinwatch_pending_roles", {}).get(str(user_id))
        if entry is None:
            return None
        projection = {
            key: entry.get(key)
            for key in (
                "incident_id",
                "source",
                "wave_id",
                "role_id",
                "verification_state",
                "test",
                "failures",
                "expires_at",
            )
        }
        key = (guild.id, user_id, entry.get("incident_id"), entry.get("failures", 0))
        projection["ready"] = bool(entry.get("incident_id") and self.preparation.get(key))
        return projection

    async def release_id(
        self,
        guild,
        user_id,
        *,
        incident_id=None,
        outcome="rollback",
        moderator_id=None,
        reason=None,
    ):
        member = await self.cog._get_member_or_fetch(guild, user_id)
        if member is not None:
            return await self.release(
                member,
                incident_id=incident_id,
                outcome=outcome,
                moderator_id=moderator_id,
                reason=reason,
            )
        async with joinwatch_state.member_lock(self.cog, guild.id, user_id):
            async with self.cog.config.guild(guild).joinwatch_pending_roles() as entries:
                entry = entries.get(str(user_id))
                if entry is None or (
                    incident_id is not None and entry.get("incident_id") != incident_id
                ):
                    return VerificationResult("stale")
                entry = dict(entry)
            await joinwatch_state.delete_pending_assignment(self.cog, guild, user_id)
            entry["completion_moderator"] = moderator_id
            entry["completion_reason"] = reason
            await self.finish(guild, user_id, entry, outcome)
            self.preparation.forget(
                (guild.id, user_id, entry.get("incident_id"), entry.get("failures", 0))
            )
            return self._result("complete", entry)

    async def _audit(self, member, entry, text, *, role_status=None):
        settings = await self._settings(member.guild)
        channel = self.cog._get_text_channel_or_thread(
            member.guild, entry.get("alert_channel_id") or settings.get("captcha_log_channel")
        )
        if channel is None or not self.cog._channel_is_private(member.guild, channel):
            return
        entry.setdefault("member_id", member.id)
        entry.setdefault("account_age_hours", max(0, int((datetime.now(timezone.utc) - member.created_at).total_seconds() // 3600)))
        entry["captcha_status"] = text
        # Preserve CAPTCHA status when other JoinWatch events refresh the same embed.
        config = self.cog.config.guild(member.guild)
        for name in ("joinwatch_pending_roles", "joinwatch_pending_role_assignments"):
            async with getattr(config, name)() as entries:
                current = entries.get(str(member.id))
                if current is not None and current.get("incident_id") == entry.get("incident_id"):
                    current.update({key: entry[key] for key in ("member_id", "account_age_hours", "captcha_status")})
        role = member.guild.get_role(entry["role_id"])
        if role_status is None:
            role_status = "Not confirmed"
            if entry.get("verification_state") == "active" or (role is not None and role in member.roles):
                role_status = f"<@&{entry['role_id']}> applied"
                if entry.get("verification_state") != "release_pending" and entry.get("expires_at"):
                    deadline = int(datetime.fromisoformat(entry["expires_at"]).timestamp())
                    role_status += f" until <t:{deadline}:R>"
        await joinwatch_publication.publish_joinwatch_incident(
            self.cog, member.guild, entry, role_status, destination=channel, member=member,
        )

    async def configuration_issues(self, guild, *, require_panel=True) -> tuple[str, ...]:
        settings = await self._settings(guild)
        issues = []
        role_id = settings.get("joinwatch_auto_role_id")
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            issues.append("Configure the JoinWatch auto-role")
        elif missing := self.cog._missing_role_assignment_permission(guild, role):
            issues.append(missing)
        for channel_id, label, private in (
            (settings.get("captcha_channel"), "CAPTCHA channel", False),
            (settings.get("captcha_log_channel"), "CAPTCHA moderator log", True),
        ):
            channel = (
                self.cog._get_text_channel_or_thread(guild, channel_id) if channel_id else None
            )
            if channel is None:
                issues.append(f"Configure the {label}")
                continue
            if private and not self.cog._channel_is_private(guild, channel):
                issues.append("The CAPTCHA moderator log must be private")
            missing = self.cog._missing_channel_permissions(
                guild,
                channel,
                send_messages=True,
                read_history=not private,
                attach_files=not private,
                embed_links=private,
            )
            if missing:
                issues.append(missing)
            if not private and role is not None and not channel.permissions_for(role).view_channel:
                issues.append("The JoinWatch role must be able to view the CAPTCHA channel")
        if require_panel and (issue := await self._panel_issue(guild, settings)):
            issues.append(issue)
        return tuple(issues)

    async def _panel_issue(self, guild, settings):
        channel_id = settings.get("captcha_panel_channel_id")
        channel = self.cog._get_text_channel_or_thread(guild, channel_id) if channel_id else None
        message_id = settings.get("captcha_panel_message_id")
        if channel is None or channel_id != settings.get("captcha_channel") or message_id is None:
            return "Publish a Verify panel in the configured CAPTCHA channel"
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return "The Verify panel is unavailable. Check permissions and publish it again"
        components = [child for row in message.components for child in row.children]
        if not any(
            getattr(child, "custom_id", None) == "honeypot:captcha:verify" for child in components
        ):
            return "The saved Verify panel has no Verify button. Publish it again"
        return None

    async def check_configuration(self, guild) -> None:
        issues = await self.configuration_issues(guild)
        if issues:
            raise ValueError("\n".join(issues))

    async def restore(self):
        queued = []
        for guild in self.cog.bot.guilds:
            await self.settle_history(guild, adopt=True)
            settings = await self._settings(guild)
            for user_id, entry in settings.get("joinwatch_pending_roles", {}).items():
                if entry.get("history_terminal"):
                    continue
                member = guild.get_member(int(user_id))
                if member is None:
                    continue
                if entry.get("verification_state") == "release_pending":
                    await self.start(member)
                elif entry.get("challenge") and entry.get("incident_id"):
                    queued.append((self._key(member, entry), entry["challenge"]))
                if entry.get("effect_at"):
                    await self._record_enrollment(guild, entry)
        self._start_fill(queued)

    async def settle_history(self, guild, *, adopt=False):
        settings = await self._settings(guild)
        for store_name in ("joinwatch_pending_role_assignments", "joinwatch_pending_roles"):
            for user_id, saved in settings.get(store_name, {}).items():
                if not (adopt or saved.get("history_terminal") or saved.get("history_events")
                        or saved.get("punishment_started_at")):
                    continue
                async with joinwatch_state.member_lock(self.cog, guild.id, int(user_id)):
                    current = await self.cog.config.guild(guild).get_raw(store_name, user_id, default=None)
                    if current is None:
                        continue
                    entry = dict(current)
                    if entry.get("history_terminal") or entry.get("punishment_started_at"):
                        await self.finish(guild, int(user_id), entry,
                                          entry.get("history_terminal", "action_uncertain"), store_name=store_name)
                    else:
                        await self.persist_history(guild, int(user_id), entry, store_name=store_name)

    async def enable_existing(self, guild) -> int:
        """Explicit moderator admission of existing timers, never a startup action."""
        settings = await self._settings(guild)
        queued = []
        count = 0
        for user_id in settings.get("joinwatch_pending_roles", {}):
            async with joinwatch_state.member_lock(self.cog, guild.id, int(user_id)):  # noqa: SIM117 - acquire member ownership before the Config context
                async with self.cog.config.guild(guild).joinwatch_pending_roles() as entries:
                    entry = entries.get(user_id)
                    if entry is None:
                        continue
                    entry["captcha_enabled"] = True
                    entry.setdefault("incident_id", secrets.token_hex(16))
                    entry.setdefault("stage", 0)
                    if entry.get("failures", 0) < MAX_ATTEMPTS:
                        if "challenge" not in entry:
                            entry["challenge"] = [generate_challenge(), generate_challenge()]
                        queued.append(
                            (
                                (
                                    guild.id,
                                    int(user_id),
                                    entry["incident_id"],
                                    entry.get("failures", 0),
                                ),
                                entry["challenge"],
                            )
                        )
                    count += 1
        self._start_fill(queued)
        return count

    def _start_fill(self, entries):
        if not entries:
            return
        task = asyncio.create_task(self._fill_preparation(entries))
        self._fill_tasks.add(task)
        task.add_done_callback(self._fill_tasks.discard)

    async def _fill_preparation(self, entries):
        for key, descriptors in entries:
            if key in self._deleted_challenges:
                continue
            while not self.preparation.request(key, descriptors):
                await self.preparation.wait()
                if key in self._deleted_challenges:
                    break

    async def delete_user_data(self, user_id):
        for guild in self.cog.bot.guilds:
            async with joinwatch_state.member_lock(self.cog, guild.id, user_id):
                for name in (
                    "joinwatch_pending_roles",
                    "joinwatch_pending_role_assignments",
                    "joinwatch_verified_members",
                ):
                    async with getattr(self.cog.config.guild(guild), name)() as entries:
                        entry = entries.pop(str(user_id), None)
                        if entry and entry.get("incident_id"):
                            key = (
                                guild.id,
                                user_id,
                                entry["incident_id"],
                                entry.get("failures", 0),
                            )
                            self._deleted_challenges.add(key)
                            self.preparation.forget(key)
                self._planned.pop((guild.id, user_id), None)
            # Remove actor references from active state as well as the archive.
            # Otherwise the next outcome update would restore the erased identity.
            for name in ("joinwatch_pending_roles", "joinwatch_pending_role_assignments"):
                config_store = getattr(self.cog.config.guild(guild), name)
                saved = await self.cog.config.guild(guild).get_raw(name, default={})
                for member_id in tuple(saved):
                    async with (
                        joinwatch_state.member_lock(self.cog, guild.id, int(member_id)),
                        config_store() as entries,
                    ):
                        current = entries.get(member_id, {})
                        for key in ("enrollment_moderator", "completion_moderator"):
                            if current.get(key) == user_id:
                                current[key] = None
                                current["completion_reason"] = None
        await asyncio.to_thread(self.cog._case_store.delete_verification_history, user_id=user_id)

    async def delete_guild_data(self, guild):
        config = self.cog.config.guild(guild)
        for name in (
            "joinwatch_pending_roles",
            "joinwatch_pending_role_assignments",
            "joinwatch_verified_members",
        ):
            entries = await config.get_raw(name, default={})
            for user_id, entry in entries.items():
                if entry.get("incident_id"):
                    key = (guild.id, int(user_id), entry["incident_id"], entry.get("failures", 0))
                    self._deleted_challenges.add(key)
                    self.preparation.forget(key)
            await config.clear_raw(name)
        for key in tuple(self._planned):
            if key[0] == guild.id:
                self._planned.pop(key)
        await asyncio.to_thread(self.cog._case_store.delete_verification_history, guild_id=guild.id)

    async def close(self):
        self._planned.clear()
        self._deleted_challenges.clear()
        for task in tuple(self._fill_tasks):
            task.cancel()
        await asyncio.gather(*self._fill_tasks, return_exceptions=True)
        self._fill_tasks.clear()
        await self.preparation.close()
        self._cancelled_waves.clear()
