from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import jwt

_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_ACCEPT = "application/vnd.github+json"
_USER_AGENT = "NHCogs-GitHubTickets"
_JWT_BACKDATE = timedelta(seconds=60)
_JWT_LIFETIME = timedelta(minutes=9)
_TOKEN_REFRESH_MARGIN = timedelta(minutes=1)
_HTTP_SUCCESS_MIN = 200
_HTTP_REDIRECT_MIN = 300
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_TOO_MANY_REQUESTS = 429
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


@dataclass(frozen=True, slots=True)
class GitHubAppCredentials:
    client_id: str
    app_id: int
    installation_id: int
    private_key: bytes = field(repr=False)
    webhook_secret: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    pull_request_id: int
    number: int
    title: str
    url: str
    state: str
    draft: bool
    merged: bool
    author_login: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitHubDeliverySummary:
    delivery_id: int
    guid: str
    delivered_at: datetime
    redelivery: bool
    status_code: int
    event: str
    action: str | None


class GitHubRequestError(RuntimeError):
    def __init__(
        self,
        operation: str,
        status: int | None = None,
        *,
        retryable: bool = False,
        rate_limited: bool = False,
        retry_at: datetime | None = None,
    ) -> None:
        self.operation = operation
        self.status = status
        self.retryable = retryable
        self.rate_limited = rate_limited
        self.retry_at = retry_at
        suffix = "" if status is None else f" with status {status}"
        super().__init__(f"GitHub {operation} failed{suffix}")


class GitHubAssigneeUnavailable(RuntimeError):
    def __init__(self, login: str) -> None:
        self.login = login
        super().__init__(f"GitHub did not assign {login}")


class GitHubAppClient:
    def __init__(
        self,
        credentials: GitHubAppCredentials,
        session: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._credentials = credentials
        self._session = session
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._token_lock = asyncio.Lock()

    async def get_pull_request(
        self,
        owner: str,
        repository: str,
        number: int,
    ) -> PullRequestSnapshot:
        payload = await self._request_json(
            "GET",
            f"/repos/{owner}/{repository}/pulls/{number}",
            operation="read pull request",
        )
        return PullRequestSnapshot(
            pull_request_id=_integer(payload["id"]),
            number=_integer(payload["number"]),
            title=str(payload["title"]),
            url=str(payload["html_url"]),
            state=str(payload["state"]),
            draft=bool(payload["draft"]),
            merged=bool(payload["merged"]),
            author_login=str(_mapping(payload["user"])["login"]),
            labels=tuple(str(_mapping(label)["name"]) for label in _sequence(payload["labels"])),
            assignees=tuple(
                str(_mapping(assignee)["login"]) for assignee in _sequence(payload["assignees"])
            ),
        )

    async def add_assignee(
        self,
        owner: str,
        repository: str,
        number: int,
        login: str,
    ) -> None:
        payload = await self._request_json(
            "POST",
            f"/repos/{owner}/{repository}/issues/{number}/assignees",
            operation="add assignee",
            json={"assignees": [login]},
        )
        if login.casefold() not in {candidate.casefold() for candidate in _assignee_logins(payload)}:
            raise GitHubAssigneeUnavailable(login)

    async def remove_assignee(
        self,
        owner: str,
        repository: str,
        number: int,
        login: str,
    ) -> None:
        payload = await self._request_json(
            "DELETE",
            f"/repos/{owner}/{repository}/issues/{number}/assignees",
            operation="remove assignee",
            json={"assignees": [login]},
        )
        if login.casefold() in {candidate.casefold() for candidate in _assignee_logins(payload)}:
            raise GitHubRequestError("remove assignee")

    async def list_deliveries(self, *, page: int = 1) -> tuple[GitHubDeliverySummary, ...]:
        payload = await self._app_request(
            "GET",
            f"/app/hook/deliveries?per_page=100&page={page}",
            operation="list deliveries",
            read_json=True,
        )
        deliveries = []
        for raw_delivery in _sequence(payload):
            delivery = _mapping(raw_delivery)
            action = delivery.get("action")
            deliveries.append(
                GitHubDeliverySummary(
                    delivery_id=_integer(delivery["id"]),
                    guid=str(delivery["guid"]),
                    delivered_at=_parse_datetime(delivery["delivered_at"]),
                    redelivery=bool(delivery["redelivery"]),
                    status_code=_integer(delivery["status_code"]),
                    event=str(delivery["event"]),
                    action=action if isinstance(action, str) else None,
                )
            )
        return tuple(deliveries)

    async def redeliver(self, delivery_id: int) -> None:
        await self._app_request(
            "POST",
            f"/app/hook/deliveries/{delivery_id}/attempts",
            operation="redeliver delivery",
            read_json=False,
        )

    async def _app_request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        read_json: bool,
    ) -> object:
        status, headers, payload = await self._send(
            method,
            path,
            headers=_headers(self._app_jwt()),
            operation=operation,
            read_json=read_json,
        )
        if not _HTTP_SUCCESS_MIN <= status < _HTTP_REDIRECT_MIN:
            raise _response_error(operation, status, headers, self._clock())
        return payload

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        for attempt in range(2):
            token = await self._installation_token()
            status, headers, payload = await self._send(
                method,
                path,
                headers=_headers(token),
                operation=operation,
                json=json,
                read_json=True,
            )
            if status == _HTTP_UNAUTHORIZED and attempt == 0:
                self._invalidate_token(token)
                continue
            if not _HTTP_SUCCESS_MIN <= status < _HTTP_REDIRECT_MIN:
                raise _response_error(operation, status, headers, self._clock())
            return _mapping(payload)
        raise GitHubRequestError(operation)

    async def _installation_token(self) -> str:
        now = self._clock()
        if self._token_valid(now):
            return self._token or ""
        async with self._token_lock:
            now = self._clock()
            if self._token_valid(now):
                return self._token or ""
            path = f"/app/installations/{self._credentials.installation_id}/access_tokens"
            status, headers, raw_payload = await self._send(
                "POST",
                path,
                headers=_headers(self._app_jwt()),
                operation="installation token",
                read_json=True,
            )
            if not _HTTP_SUCCESS_MIN <= status < _HTTP_REDIRECT_MIN:
                raise _response_error("installation token", status, headers, self._clock())
            payload = _mapping(raw_payload)
            self._token = str(payload["token"])
            self._token_expires_at = datetime.fromisoformat(
                str(payload["expires_at"]).replace("Z", "+00:00")
            )
            return self._token

    async def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        operation: str,
        read_json: bool,
        json: Mapping[str, object] | None = None,
    ) -> tuple[int, Mapping[str, str], object]:
        try:
            async with self._session.request(
                method,
                f"{_API_ROOT}{path}",
                headers=headers,
                json=json,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                response_headers = {
                    str(name).casefold(): str(value) for name, value in response.headers.items()
                }
                payload = (
                    await response.json(content_type=None)
                    if read_json and _HTTP_SUCCESS_MIN <= response.status < _HTTP_REDIRECT_MIN
                    else None
                )
                return int(response.status), response_headers, payload
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise GitHubRequestError(operation, retryable=True) from None

    def _app_jwt(self) -> str:
        now = self._clock()
        return jwt.encode(
            {
                "iat": int((now - _JWT_BACKDATE).timestamp()),
                "exp": int((now + _JWT_LIFETIME).timestamp()),
                "iss": self._credentials.client_id,
            },
            self._credentials.private_key,
            algorithm="RS256",
        )

    def _token_valid(self, now: datetime) -> bool:
        return (
            self._token is not None
            and self._token_expires_at is not None
            and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN
        )

    def _invalidate_token(self, rejected_token: str) -> None:
        if self._token == rejected_token:
            self._token = None
            self._token_expires_at = None


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": _ACCEPT,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": _USER_AGENT,
    }


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubRequestError("decode response")
    return value


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise GitHubRequestError("decode response")
    return tuple(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise GitHubRequestError("decode response")
    return value


def _assignee_logins(payload: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        str(_mapping(assignee)["login"]) for assignee in _sequence(payload["assignees"])
    )


def _parse_datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _response_error(
    operation: str,
    status: int,
    headers: Mapping[str, str],
    now: datetime,
) -> GitHubRequestError:
    retry_after = headers.get("retry-after")
    rate_limit_reset = headers.get("x-ratelimit-reset")
    rate_limited = status == _HTTP_TOO_MANY_REQUESTS or (
        status == _HTTP_FORBIDDEN and (retry_after is not None or rate_limit_reset is not None)
    )
    retry_at = _retry_at(retry_after, rate_limit_reset, now) if rate_limited else None
    return GitHubRequestError(
        operation,
        status,
        retryable=rate_limited or status in _TRANSIENT_STATUSES,
        rate_limited=rate_limited,
        retry_at=retry_at,
    )


def _retry_at(retry_after: object, rate_limit_reset: object, now: datetime) -> datetime | None:
    if retry_after is not None:
        try:
            return now + timedelta(seconds=max(0.0, float(str(retry_after))))
        except ValueError:
            pass
    if rate_limit_reset is not None:
        try:
            return datetime.fromtimestamp(float(str(rate_limit_reset)), tz=timezone.utc)
        except ValueError:
            pass
    return None
