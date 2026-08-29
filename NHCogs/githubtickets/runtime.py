from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, TypeVar

from NHCogs.operational_errors import report_operational_error

from .github_app import (
    GitHubAppClient,
    GitHubAssigneeUnavailable,
    GitHubDeliverySummary,
    GitHubRequestError,
)
from .models import (
    GitHubDelivery,
    GitHubDeliveryState,
    GitHubOutboxItem,
    GitHubOutboxOperation,
)
from .store import DELIVERY_IDENTITY_RETENTION, GitHubTicketsStore
from .webhook import GitHubWebhookReceiver

_DELIVERIES_PER_PAGE = 100
_HTTP_SUCCESS_MIN = 200
_HTTP_REDIRECT_MIN = 300
_ResultT = TypeVar("_ResultT")


class DeliveryDisposition(str, Enum):
    PROCESSED = "processed"
    IGNORED = "ignored"
    STOPPED = "stopped"


class GitHubIntegrationRuntime:
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        client: GitHubAppClient | None,
        receiver: GitHubWebhookReceiver | None,
        delivery_handler: Callable[[GitHubDelivery], Awaitable[DeliveryDisposition]],
        bot: Any,
        guild_id: int,
        clock: Callable[[], datetime],
        poll_interval: float = 1.0,
        recovery_interval: timedelta = timedelta(minutes=15),
        stale_after: timedelta = timedelta(minutes=5),
        retry_base: timedelta = timedelta(seconds=30),
        retry_cap: timedelta = timedelta(minutes=15),
        max_delivery_attempts: int = 5,
        max_outbox_attempts: int = 5,
        max_recovery_pages: int = 10,
        max_redeliveries_per_recovery: int = 100,
        random_source: random.Random | None = None,
    ) -> None:
        if (client is None) != (receiver is None):
            raise ValueError("GitHub client and webhook receiver must be configured together")
        if poll_interval < 0:
            raise ValueError("poll interval cannot be negative")
        if recovery_interval <= timedelta(0) or stale_after <= timedelta(0):
            raise ValueError("recovery and stale intervals must be positive")
        if retry_base <= timedelta(0) or retry_cap < retry_base:
            raise ValueError("retry delays must be positive and ordered")
        if max_delivery_attempts < 1:
            raise ValueError("delivery attempt limit must be positive")
        if max_outbox_attempts < 1:
            raise ValueError("outbox attempt limit must be positive")
        if max_recovery_pages < 1 or max_redeliveries_per_recovery < 1:
            raise ValueError("recovery limits must be positive")
        self._store = store
        self._client = client
        self._receiver = receiver
        self._delivery_handler = delivery_handler
        self._bot = bot
        self._guild_id = guild_id
        self._clock = clock
        self._poll_interval = poll_interval
        self._recovery_interval = recovery_interval
        self._stale_after = stale_after
        self._retry_base = retry_base
        self._retry_cap = retry_cap
        self._max_delivery_attempts = max_delivery_attempts
        self._max_outbox_attempts = max_outbox_attempts
        self._max_recovery_pages = max_recovery_pages
        self._max_redeliveries_per_recovery = max_redeliveries_per_recovery
        self._random = random_source or random.Random()
        self._github_mutation_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovery_requested = asyncio.Event()
        self._recovery_not_before: datetime | None = None
        self._stop_requested = asyncio.Event()
        self._token_invalidated = False
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False

    async def start(self, host: str, port: int) -> int | None:
        if self._started:
            raise RuntimeError("GitHub integration runtime is already running")
        self._stop_requested.clear()
        self._token_invalidated = False
        self._started = True
        if self._receiver is None:
            return None
        try:
            bound_port = await self._receiver.start(host, port)
        except BaseException:
            self._started = False
            raise
        self._tasks = [
            asyncio.create_task(
                self._guard_background("process webhook deliveries", self._delivery_loop),
                name="githubtickets-deliveries",
            ),
            asyncio.create_task(
                self._guard_background("process GitHub outbox", self._outbox_loop),
                name="githubtickets-outbox",
            ),
            asyncio.create_task(
                self._guard_background("recover GitHub deliveries", self._recovery_loop),
                name="githubtickets-recovery",
            ),
        ]
        return bound_port

    async def close(self) -> None:
        if not self._started and not self._tasks:
            return
        self._stop_requested.set()
        try:
            if self._receiver is not None:
                await self._receiver.close()
        finally:
            current = asyncio.current_task()
            tasks = [task for task in self._tasks if task is not current]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._invalidate_client_token()
            self._tasks = [
                task for task in self._tasks if task is current and not task.done()
            ]
            self._started = False

    async def run_recovery(self) -> None:
        if self._client is None:
            return
        if self._recovery_lock.locked():
            return
        async with self._recovery_lock:
            await self._run_recovery_once()

    async def _run_recovery_once(self) -> None:
        if (
            self._recovery_not_before is not None
            and self._clock() < self._recovery_not_before
        ):
            return
        self._recovery_not_before = None
        try:
            await self._recover_deliveries()
        finally:
            try:
                await self._await_store(self._store.prune_deliveries(self._clock()))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._report("prune GitHub webhook deliveries", error)

    def request_recovery(self) -> None:
        self._recovery_requested.set()

    async def _recover_deliveries(self) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("GitHub integration is not configured")
        redeliveries = 0
        identity_cutoff = self._clock() - DELIVERY_IDENTITY_RETENTION
        start_page, resume_after_delivery_id = await self._await_store(
            self._store.get_delivery_recovery_checkpoint()
        )
        for page in range(start_page, start_page + self._max_recovery_pages):
            try:
                deliveries = await client.list_deliveries(page=page)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._report("list GitHub webhook deliveries", error)
                self._apply_recovery_failure(error)
                return
            listed_count = len(deliveries)
            if resume_after_delivery_id is not None:
                resume_index = next(
                    (
                        index
                        for index, delivery in enumerate(deliveries)
                        if delivery.delivery_id == resume_after_delivery_id
                    ),
                    None,
                )
                if resume_index is not None:
                    deliveries = deliveries[resume_index + 1 :]
                resume_after_delivery_id = None
            attempted, last_checked_delivery_id, stopped = (
                await self._recover_delivery_page(
                    deliveries,
                    identity_cutoff=identity_cutoff,
                    remaining_redeliveries=(
                        self._max_redeliveries_per_recovery - redeliveries
                    ),
                )
            )
            redeliveries += attempted
            if stopped:
                await self._save_recovery_checkpoint(
                    next_page=page,
                    last_delivery_id=last_checked_delivery_id,
                )
                return
            if listed_count < _DELIVERIES_PER_PAGE:
                await self._save_recovery_checkpoint(
                    next_page=1,
                    last_delivery_id=None,
                )
                return
            await self._save_recovery_checkpoint(
                next_page=page + 1,
                last_delivery_id=last_checked_delivery_id,
            )

    async def _recover_delivery_page(
        self,
        deliveries: tuple[GitHubDeliverySummary, ...],
        *,
        identity_cutoff: datetime,
        remaining_redeliveries: int,
    ) -> tuple[int, int | None, bool]:
        client = self._client
        if client is None:
            raise RuntimeError("GitHub integration is not configured")
        attempted = 0
        last_checked_delivery_id: int | None = None
        for delivery in deliveries:
            failed_summary = not (
                _HTTP_SUCCESS_MIN <= delivery.status_code < _HTTP_REDIRECT_MIN
            )
            if delivery.delivered_at < identity_cutoff or (
                delivery.redelivery and not failed_summary
            ):
                last_checked_delivery_id = delivery.delivery_id
                continue
            try:
                local_delivery = await self._await_store(
                    self._store.get_delivery(delivery.guid)
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._report(
                    f"inspect GitHub webhook delivery {delivery.delivery_id}",
                    error,
                )
                last_checked_delivery_id = delivery.delivery_id
                continue
            should_redeliver = (
                local_delivery is None
                or local_delivery.state
                in {GitHubDeliveryState.FAILED, GitHubDeliveryState.RETRY}
                or failed_summary
            )
            if not should_redeliver:
                last_checked_delivery_id = delivery.delivery_id
                continue
            if attempted >= remaining_redeliveries:
                return attempted, last_checked_delivery_id, True
            attempted += 1
            try:
                async with self._github_mutation_lock:
                    await client.redeliver(delivery.delivery_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._report(
                    f"redeliver GitHub webhook delivery {delivery.delivery_id}",
                    error,
                )
                if self._apply_recovery_failure(error):
                    return attempted, last_checked_delivery_id, True
            last_checked_delivery_id = delivery.delivery_id
        return attempted, last_checked_delivery_id, False

    async def _guard_background(
        self,
        action: str,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        while not self._stop_requested.is_set():
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._report(action, error)
                await asyncio.sleep(self._poll_interval)

    async def _delivery_loop(self) -> None:
        while not self._stop_requested.is_set():
            now = self._clock()
            delivery = await self._await_store(
                self._store.claim_next_delivery(
                    now=now,
                    stale_before=now - self._stale_after,
                )
            )
            if delivery is None:
                await asyncio.sleep(self._poll_interval)
                continue
            await self._process_delivery(delivery)

    async def _process_delivery(self, delivery: GitHubDelivery) -> None:
        try:
            disposition = await self._delivery_handler(delivery)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._report(
                f"process GitHub delivery {delivery.delivery_guid}",
                error,
            )
            summary = _error_summary(error)
            retryable, _rate_limited, retry_at = _classify_github_request(error)
            terminal = isinstance(error, GitHubRequestError) and not retryable
            if terminal or delivery.attempts >= self._max_delivery_attempts:
                await self._await_store(
                    self._store.fail_delivery(
                        delivery.delivery_guid,
                        completed_at=self._clock(),
                        error_summary=summary,
                    )
                )
            else:
                await self._await_store(
                    self._store.defer_delivery(
                        delivery.delivery_guid,
                        next_attempt_at=self._next_retry_at(
                            delivery.attempts,
                            retry_at,
                        ),
                        error_summary=summary,
                    )
                )
            return
        await self._await_store(
            self._store.complete_delivery(
                delivery.delivery_guid,
                completed_at=self._clock(),
                ignored=disposition is DeliveryDisposition.IGNORED,
            )
        )
        if disposition is DeliveryDisposition.STOPPED:
            await self._stop_after_lifecycle_delivery(delivery)

    async def _stop_after_lifecycle_delivery(self, delivery: GitHubDelivery) -> None:
        self._stop_requested.set()
        if self._receiver is not None:
            await self._receiver.close()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = [task for task in self._tasks if task is current and not task.done()]
        self._invalidate_client_token()
        self._started = False
        await self._report(
            f"stop GitHub integration after {delivery.event} {delivery.action or 'event'}",
            RuntimeError("GitHub App lifecycle changed"),
        )

    def _invalidate_client_token(self) -> None:
        if self._client is None or self._token_invalidated:
            return
        self._client.invalidate_installation_token()
        self._token_invalidated = True

    def _next_retry_at(
        self,
        attempts: int,
        retry_at: datetime | None = None,
    ) -> datetime:
        base_seconds = self._retry_base.total_seconds()
        cap_seconds = self._retry_cap.total_seconds()
        exponential = base_seconds * (2 ** min(max(attempts - 1, 0), 30))
        bounded = min(exponential, cap_seconds)
        jittered = bounded * (0.5 + self._random.random())
        calculated = self._clock() + timedelta(seconds=min(jittered, cap_seconds))
        if retry_at is not None and retry_at > calculated:
            return retry_at
        return calculated

    async def _outbox_loop(self) -> None:
        while not self._stop_requested.is_set():
            now = self._clock()
            item = await self._await_store(
                self._store.claim_next_outbox(
                    now=now,
                    stale_before=now - self._stale_after,
                )
            )
            if item is None:
                await asyncio.sleep(self._poll_interval)
                continue
            await self._process_outbox(item)

    async def _process_outbox(self, item: GitHubOutboxItem) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("GitHub integration is not configured")
        try:
            owner, repository = _split_repository_name(item.repository_full_name)
            async with self._github_mutation_lock:
                if item.operation is GitHubOutboxOperation.ADD_ASSIGNEE:
                    await client.add_assignee(
                        owner,
                        repository,
                        item.pr_number,
                        item.github_login,
                    )
                elif item.operation is GitHubOutboxOperation.REMOVE_ASSIGNEE:
                    await client.remove_assignee(
                        owner,
                        repository,
                        item.pr_number,
                        item.github_login,
                    )
                else:
                    raise ValueError(f"unsupported GitHub outbox operation {item.operation}")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._report(f"apply GitHub outbox item {item.outbox_id}", error)
            summary = _error_summary(error)
            terminal = isinstance(error, (GitHubAssigneeUnavailable, ValueError))
            retry_at = None
            if isinstance(error, GitHubRequestError):
                retryable, _rate_limited, retry_at = _classify_github_request(error)
                terminal = not retryable
            if terminal or item.attempts >= self._max_outbox_attempts:
                await self._await_store(
                    self._store.fail_outbox(
                        item.outbox_id,
                        failed_at=self._clock(),
                        error_summary=summary,
                    )
                )
            else:
                await self._await_store(
                    self._store.defer_outbox(
                        item.outbox_id,
                        next_attempt_at=self._next_retry_at(item.attempts, retry_at),
                        error_summary=summary,
                    )
                )
            return
        await self._await_store(
            self._store.complete_outbox(
                item.outbox_id,
                completed_at=self._clock(),
            )
        )

    async def _recovery_loop(self) -> None:
        while not self._stop_requested.is_set():
            self._recovery_requested.clear()
            await self.run_recovery()
            try:
                timeout = self._recovery_interval.total_seconds()
                if self._recovery_not_before is not None:
                    timeout = max(
                        timeout,
                        (self._recovery_not_before - self._clock()).total_seconds(),
                    )
                await asyncio.wait_for(
                    self._recovery_requested.wait(),
                    timeout=max(0.0, timeout),
                )
            except asyncio.TimeoutError:
                pass

    def _apply_recovery_failure(self, error: BaseException) -> bool:
        _retryable, rate_limited, retry_at = _classify_github_request(error)
        if not rate_limited:
            return False
        self._recovery_not_before = retry_at or self._next_retry_at(1)
        return True

    async def _save_recovery_checkpoint(
        self,
        *,
        next_page: int,
        last_delivery_id: int | None,
    ) -> None:
        try:
            await self._await_store(
                self._store.save_delivery_recovery_checkpoint(
                    next_page=next_page,
                    last_delivery_id=last_delivery_id,
                    checked_at=self._clock(),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._report("save GitHub delivery recovery checkpoint", error)

    async def _await_store(self, operation: Awaitable[_ResultT]) -> _ResultT:
        task = asyncio.ensure_future(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _report(self, action: str, error: BaseException) -> None:
        await report_operational_error(
            self._bot,
            guild_id=self._guild_id,
            source="GitHubTickets",
            action=action,
            error=error,
        )


def _error_summary(error: BaseException) -> str:
    if isinstance(error, (GitHubAssigneeUnavailable, GitHubRequestError)):
        detail = " ".join(str(error).split())
        return f"{type(error).__name__}: {detail}"[:500]
    return type(error).__name__[:500]


def _classify_github_request(
    error: BaseException,
) -> tuple[bool, bool, datetime | None]:
    if not isinstance(error, GitHubRequestError):
        return True, False, None
    return (
        error.retryable or error.rate_limited,
        error.rate_limited,
        error.retry_at,
    )


def _split_repository_name(full_name: str) -> tuple[str, str]:
    if full_name.count("/") != 1:
        raise ValueError("GitHub repository full name must contain one slash")
    owner, repository = full_name.split("/", 1)
    if not owner or not repository:
        raise ValueError("GitHub repository full name must include owner and repository")
    return owner, repository
