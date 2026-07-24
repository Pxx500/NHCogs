from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import logging
import time
from typing import Any, Callable

from .role_analytics_store import (
    MemberSnapshot,
    RoleAnalyticsStore,
    SyncStatus,
)


class SyncAlreadyRunningError(RuntimeError):
    """Raised when a guild already has an active full synchronization."""


class MemberIntentRequiredError(RuntimeError):
    """Raised when the privileged members intent is unavailable."""


class AnalyticsDisabledError(RuntimeError):
    """Raised when automatic work is requested for a disabled guild."""


class FullMemberRequestCooldownError(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        super().__init__("A full member request is still on cooldown")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class SyncResult:
    generation: int
    member_count: int
    membership_count: int
    source: str
    elapsed_seconds: float


class RoleAnalyticsService:
    def __init__(
        self,
        bot: Any,
        store: RoleAnalyticsStore,
        *,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._store = store
        self._log = logger or logging.getLogger(__name__)
        self._monotonic = monotonic
        self._sync_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._event_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sync_generations: dict[int, int] = {}
        self._event_queues: dict[int, list[tuple[str, object]]] = defaultdict(list)
        self._last_full_request: dict[int, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._guild_tasks: dict[int, set[asyncio.Task]] = defaultdict(set)
        self._resumed_task: asyncio.Task | None = None

    def is_syncing(self, guild_id: int) -> bool:
        return self._sync_locks[int(guild_id)].locked()

    async def sync_guild(self, guild: Any, *, manual: bool) -> SyncResult:
        guild_id = int(guild.id)
        lock = self._sync_locks[guild_id]
        if lock.locked():
            raise SyncAlreadyRunningError("A role synchronization is already running")

        async with lock:
            state = await self._store.get_state(guild_id)
            if not manual and not state.enabled:
                raise AnalyticsDisabledError("Role analytics are disabled")
            if not bool(
                getattr(getattr(self._bot, "intents", None), "members", False)
            ):
                await self._store.set_status(
                    guild_id,
                    SyncStatus.FAILED,
                    "missing_members_intent",
                )
                raise MemberIntentRequiredError("The members intent is required")

            started = self._monotonic()
            if not bool(guild.chunked):
                last_request = self._last_full_request.get(guild_id)
                if last_request is not None:
                    retry_after = 30.0 - (started - last_request)
                    if retry_after > 0:
                        await self._store.set_status(
                            guild_id,
                            SyncStatus.RETRYING,
                            "member_request_cooldown",
                        )
                        raise FullMemberRequestCooldownError(retry_after)
            generation: int | None = None
            source = "existing-cache"
            try:
                generation = await self._store.next_generation(guild_id)
                async with self._event_locks[guild_id]:
                    self._sync_generations[guild_id] = generation
                    self._event_queues[guild_id].clear()

                if not bool(guild.chunked):
                    source = "gateway-chunk"
                    self._last_full_request[guild_id] = self._monotonic()
                    await guild.chunk(cache=True)

                default_role_id = int(guild.default_role.id)
                members = tuple(
                    self._snapshot_member(member, default_role_id)
                    for member in tuple(guild.members)
                )
                await self._store.write_generation(
                    guild_id,
                    generation,
                    members,
                )
                await self._replay_and_activate(
                    guild_id,
                    generation,
                    len(members),
                )
                await self._store.delete_inactive_generations(guild_id)
                return SyncResult(
                    generation=generation,
                    member_count=len(members),
                    membership_count=sum(len(member.role_ids) for member in members),
                    source=source,
                    elapsed_seconds=self._monotonic() - started,
                )
            except Exception:
                async with self._event_locks[guild_id]:
                    self._sync_generations.pop(guild_id, None)
                    self._event_queues[guild_id].clear()
                if generation is not None:
                    await self._store.discard_generation(guild_id, generation)
                await self._store.set_status(
                    guild_id,
                    SyncStatus.FAILED,
                    "sync_failed",
                )
                raise

    async def member_joined(
        self,
        guild_id: int,
        member: Any,
        default_role_id: int,
    ) -> None:
        await self._submit_event(
            guild_id,
            "replace",
            self._snapshot_member(member, default_role_id),
        )

    async def member_roles_changed(
        self,
        guild_id: int,
        member: Any,
        default_role_id: int,
    ) -> None:
        await self.member_joined(guild_id, member, default_role_id)

    async def member_removed(self, guild_id: int, user_id: int) -> None:
        await self._submit_event(guild_id, "remove_member", int(user_id))

    async def role_deleted(self, guild_id: int, role_id: int) -> None:
        await self._submit_event(guild_id, "remove_role", int(role_id))

    async def reconcile_enabled_guilds(
        self,
        guilds: list[Any] | tuple[Any, ...],
    ) -> tuple[SyncResult, ...]:
        enabled_guilds: list[Any] = []
        for guild in tuple(guilds):
            if await self._store.mark_needs_reconciliation_if_enabled(
                int(guild.id)
            ):
                enabled_guilds.append(guild)
        return await self._reconcile_marked_guilds(tuple(enabled_guilds))

    async def _reconcile_marked_guilds(
        self,
        guilds: tuple[Any, ...],
    ) -> tuple[SyncResult, ...]:
        results: list[SyncResult] = []
        for guild in guilds:
            try:
                results.append(await self.sync_guild(guild, manual=False))
            except AnalyticsDisabledError:
                continue
            except SyncAlreadyRunningError:
                continue
            except FullMemberRequestCooldownError as error:
                self._log.warning(
                    "Role analytics reconciliation for guild %s is waiting %.1fs",
                    guild.id,
                    error.retry_after,
                )
                self.schedule_guild_retry(guild, error.retry_after)
            except MemberIntentRequiredError:
                self._log.error(
                    "Role analytics reconciliation requires members intent for guild %s",
                    guild.id,
                )
            except Exception:
                self._log.exception(
                    "Role analytics reconciliation failed for guild %s",
                    guild.id,
                )
                await self._store.set_status(
                    int(guild.id),
                    SyncStatus.RETRYING,
                    "sync_retry_scheduled",
                )
                self.schedule_guild_retry(guild, 30.0)
        return tuple(results)

    async def run_daily_reconciliation(
        self,
        guilds: list[Any] | tuple[Any, ...],
    ) -> tuple[SyncResult, ...]:
        return await self.reconcile_enabled_guilds(guilds)

    async def schedule_resumed_check(
        self,
        guilds: list[Any] | tuple[Any, ...],
        *,
        delay: float = 5.0,
    ) -> asyncio.Task:
        if self._resumed_task is not None and not self._resumed_task.done():
            self._resumed_task.cancel()

        enabled_guilds: list[Any] = []
        for guild in tuple(guilds):
            if await self._store.mark_needs_reconciliation_if_enabled(
                int(guild.id)
            ):
                enabled_guilds.append(guild)
        marked_guilds = tuple(enabled_guilds)

        async def run() -> tuple[SyncResult, ...]:
            await asyncio.sleep(delay)
            return await self._reconcile_marked_guilds(marked_guilds)

        self._resumed_task = self._track_task(asyncio.create_task(run()))
        return self._resumed_task

    def schedule_guild_retry(self, guild: Any, delay: float) -> asyncio.Task:
        guild_id = int(guild.id)
        existing = next(
            (
                task
                for task in self._guild_tasks.get(guild_id, ())
                if not task.done()
            ),
            None,
        )
        if existing is not None:
            return existing

        async def run() -> SyncResult | None:
            next_delay = max(0.0, delay)
            attempt = 0
            while True:
                await asyncio.sleep(next_delay)
                try:
                    return await self.sync_guild(guild, manual=False)
                except (AnalyticsDisabledError, MemberIntentRequiredError):
                    return None
                except FullMemberRequestCooldownError as error:
                    next_delay = error.retry_after
                except Exception:
                    attempt += 1
                    exponent = min(attempt - 1, 7)
                    next_delay = min(30.0 * (2**exponent), 3600.0)
                    self._log.exception(
                        "Scheduled role analytics retry failed for guild %s; retrying in %.1fs",
                        guild.id,
                        next_delay,
                    )
                await self._store.set_status(
                    guild_id,
                    SyncStatus.RETRYING,
                    "sync_retry_scheduled",
                )

        task = self._track_task(asyncio.create_task(run()))
        self._guild_tasks[guild_id].add(task)

        def discard_guild_task(done: asyncio.Task) -> None:
            tasks = self._guild_tasks.get(guild_id)
            if tasks is None:
                return
            tasks.discard(done)
            if not tasks:
                self._guild_tasks.pop(guild_id, None)

        task.add_done_callback(discard_guild_task)
        return task

    async def disable_guild(self, guild_id: int) -> None:
        guild_id = int(guild_id)
        for task in tuple(self._guild_tasks.pop(guild_id, ())):
            task.cancel()
        async with self._sync_locks[guild_id]:
            await self._store.clear_guild(guild_id)

    def cancel(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._guild_tasks.clear()
        self._resumed_task = None

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _submit_event(
        self,
        guild_id: int,
        event_type: str,
        payload: object,
    ) -> None:
        async with self._event_locks[guild_id]:
            if guild_id in self._sync_generations:
                self._event_queues[guild_id].append((event_type, payload))
                return
            state = await self._store.get_state(guild_id)
            if not state.enabled or state.status != SyncStatus.READY:
                return
            await self._apply_event(
                guild_id,
                int(state.active_generation),
                event_type,
                payload,
            )

    @staticmethod
    def _snapshot_member(member: Any, default_role_id: int) -> MemberSnapshot:
        return MemberSnapshot(
            user_id=int(member.id),
            is_bot=bool(member.bot),
            role_ids=tuple(
                sorted(
                    int(role.id)
                    for role in member.roles
                    if int(role.id) != default_role_id
                )
            ),
        )

    async def _replay_and_activate(
        self,
        guild_id: int,
        generation: int,
        source_member_count: int,
    ) -> None:
        while True:
            async with self._event_locks[guild_id]:
                events = tuple(self._event_queues[guild_id])
                self._event_queues[guild_id].clear()
                if not events:
                    await self._store.activate_generation(
                        guild_id,
                        generation,
                        source_member_count,
                    )
                    self._sync_generations.pop(guild_id, None)
                    return
            for event_type, payload in events:
                await self._apply_event(
                    guild_id,
                    generation,
                    event_type,
                    payload,
                )

    async def _apply_event(
        self,
        guild_id: int,
        generation: int,
        event_type: str,
        payload: object,
    ) -> None:
        if event_type == "replace":
            await self._store.replace_member(
                guild_id,
                payload,
                generation,
            )
        elif event_type == "remove_member":
            await self._store.remove_member(guild_id, int(payload), generation)
        elif event_type == "remove_role":
            await self._store.remove_role(guild_id, int(payload), generation)
