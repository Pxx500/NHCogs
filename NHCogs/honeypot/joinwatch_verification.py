"""JoinWatch-owned CAPTCHA enrollment, attempts, and exact-incident release."""

from __future__ import annotations

import asyncio
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from . import joinwatch_state
from .captcha import CaptchaPreparation, CaptchaQuestion, generate_challenge
from .settings import DEFAULTS

MAX_ATTEMPTS = 2
PREPARED_ENROLLMENT_CAPACITY = 32


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
            *(config.get_raw(name, default=DEFAULTS.get(name)) for name in names)
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
            return VerificationResult("active")
        if str(member.id) in settings.get("joinwatch_verified_members", {}):
            return VerificationResult("verified")
        role = member.guild.get_role(settings.get("joinwatch_auto_role_id"))
        if role is None:
            return VerificationResult("unavailable")
        if role in member.roles:
            return VerificationResult("ambiguous")
        if self.cog._missing_role_assignment_permission(member.guild, role):
            return VerificationResult("unavailable")
        if settings.get("dry_run"):
            return VerificationResult("dry_run")
        return VerificationResult("eligible")

    async def prepare_enrollment(self, member, *, source="wave", reasons=(), wave_id=None):
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
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
                    await self._audit(member, "CAPTCHA image preparation failed")
                    return self._result("error", entry)
                self.preparation.request(preparation_key, entry["challenge"])
            return self._result("ready" if ready else "preparing", entry)

    async def enroll_prepared(self, member, *, source="wave", reasons=(), wave_id=None):  # noqa: PLR0911 - enrollment validation precedes effects
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
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
            entry.update(
                {
                    "applied_at": now.isoformat(),
                    "expires_at": (
                        now
                        + timedelta(minutes=settings.get("joinwatch_auto_role_timer_minutes", 1440))
                    ).isoformat(),
                    "verification_state": "enrolling",
                    "role_owned": False,
                    "member_id": member.id,
                }
            )
            await self._save(member, entry)
            role = member.guild.get_role(entry["role_id"])
            try:
                await member.add_roles(role, reason="Automated account status update.")
            except discord.HTTPException as error:
                if getattr(error, "status", None) in (400, 403, 404):
                    await joinwatch_state.delete_pending_role(self.cog, member.guild, member.id)
                await self._audit(member, "CAPTCHA restriction application failed")
                return self._result("error", entry)
            entry.update(
                {
                    "verification_state": "active",
                    "role_owned": True,
                    "effect_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._save(member, entry, expected=entry["incident_id"])
            self._planned.pop(key, None)
            await self._record_enrollment(member.guild, entry)
            await self._audit(member, f"CAPTCHA enrolled ({source})")
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
        result = await self.enroll_prepared(member, source="test", reasons=("test",))
        if result.status == "enrolled":
            await self._audit(member, "CAPTCHA test started", moderator_id=moderator_id)
        return result

    async def prepare_assignment(self, member, incident: dict) -> None:
        """Prepare during the existing delay without changing either timestamp."""
        if not incident.get("incident_id"):
            incident["incident_id"] = secrets.token_hex(16)
        incident["captcha_enabled"] = True
        incident.setdefault("source", "join")
        incident.setdefault("reasons", ["age"])
        incident.setdefault("failures", 0)
        incident.setdefault("stage", 0)
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
            if allowed.status not in ("eligible", "active"):
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
                return self._result("active", entry)
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

    async def start(self, member, *, now=None) -> VerificationResult:  # noqa: PLR0911 - active incident guards precede session exposure
        now = now or datetime.now(timezone.utc)
        async with joinwatch_state.member_lock(self.cog, member.guild.id, member.id):
            entry = await self._entry(member)
            if entry is None or not entry.get("captcha_enabled"):
                return VerificationResult("unavailable")
            if member.bot or await self.cog._is_protected_member(member):
                return self._result("protected", entry)
            if entry.get("verification_state") == "release_pending":
                return await self._release_locked(member, entry)
            if entry.get("verification_state") == "enrolling":
                return self._result("error", entry)
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
                    await self._audit(member, "CAPTCHA image preparation failed")
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
                await self._save(member, entry, expected=entry["incident_id"])
                return self._result(
                    "question", entry, question=questions[1], session_id=session_id, stage=1
                )
            entry["verification_state"] = "release_pending"
            entry["completion_outcome"] = "passed"
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
        retained = await self._independent_role_reason(member, entry["role_id"])
        if role is not None and role in member.roles and not retained:
            if not entry.get("role_owned"):
                return self._result("ambiguous", entry)
            if self.cog._missing_role_assignment_permission(member.guild, role):
                return self._result("release_pending", entry)
            try:
                await member.remove_roles(role, reason="Account verification completed.")
            except discord.HTTPException:
                await self._audit(member, "CAPTCHA restriction release failed")
                return self._result("release_pending", entry)
        if not entry.get("test") and entry.get("completion_outcome") == "passed":
            async with self.cog.config.guild(member.guild).joinwatch_verified_members() as verified:
                verified[str(member.id)] = {
                    "incident_id": entry["incident_id"],
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
        async with self.cog.config.guild(member.guild).joinwatch_pending_roles() as entries:
            current = entries.get(str(member.id))
            if current is None or current.get("incident_id") != entry["incident_id"]:
                return VerificationResult("stale")
            entries.pop(str(member.id))
        await joinwatch_state.delete_pending_assignment(self.cog, member.guild, member.id)
        self.preparation.forget(self._key(member, entry))
        await self._audit(
            member,
            f"CAPTCHA completed ({entry.get('completion_outcome', 'manual')})",
            moderator_id=entry.get("completion_moderator"),
            reason=entry.get("completion_reason"),
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
                "role_owned",
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
                entries.pop(str(user_id))
            await joinwatch_state.delete_pending_assignment(self.cog, guild, user_id)
            self.preparation.forget(
                (guild.id, user_id, entry.get("incident_id"), entry.get("failures", 0))
            )
            return self._result("complete", entry)

    async def _audit(self, member, text, *, moderator_id=None, reason=None):
        settings = await self._settings(member.guild)
        channel = self.cog._get_text_channel_or_thread(
            member.guild, settings.get("captcha_log_channel")
        )
        if channel is None or not self.cog._channel_is_private(member.guild, channel):
            return
        message = f"{text}: user {member.id}"
        if moderator_id is not None:
            message += f", moderator {moderator_id}"
        if reason:
            message += f", reason: {reason}"
        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await self.cog._record_operational_failure(
                member.guild.id, "captcha_audit", "Could not publish CAPTCHA audit"
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
            settings = await self._settings(guild)
            for user_id, entry in settings.get("joinwatch_pending_roles", {}).items():
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

    async def enable_existing(self, guild) -> int:
        """Explicit moderator admission of existing timers, never a startup action."""
        settings = await self._settings(guild)
        queued = []
        count = 0
        for user_id in settings.get("joinwatch_pending_roles", {}):
            async with joinwatch_state.member_lock(self.cog, guild.id, int(user_id)):  # noqa: SIM117 - acquire member ownership before the Config context
                async with self.cog.config.guild(guild).joinwatch_pending_roles() as entries:
                    entry = entries.get(user_id)
                    if entry is None or entry.get("verification_state") == "enrolling":
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

    async def close(self):
        self._planned.clear()
        self._deleted_challenges.clear()
        for task in tuple(self._fill_tasks):
            task.cancel()
        await asyncio.gather(*self._fill_tasks, return_exceptions=True)
        self._fill_tasks.clear()
        await self.preparation.close()
