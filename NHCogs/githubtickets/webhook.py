from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timezone

from aiohttp import web

from .github_app import GitHubAppCredentials
from .store import GitHubTicketsStore

WEBHOOK_PATH = "/githubtickets/webhook"
_MAX_BODY_BYTES = 1024 * 1024
_PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_review"})


class GitHubWebhookReceiver:
    def __init__(
        self,
        store: GitHubTicketsStore,
        credentials: GitHubAppCredentials,
        *,
        organization: str,
    ) -> None:
        self._store = store
        self._credentials = credentials
        self._organization = organization.casefold()
        self.application = web.Application(client_max_size=_MAX_BODY_BYTES)
        self.application.router.add_post(WEBHOOK_PATH, self._receive)
        self._runner: web.AppRunner | None = None

    async def start(self, host: str, port: int) -> int:
        if self._runner is not None:
            raise RuntimeError("GitHub webhook receiver is already running")
        runner = web.AppRunner(self.application)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        return port

    async def close(self) -> None:
        runner = self._runner
        if runner is None:
            return
        await runner.cleanup()
        self._runner = None

    async def _receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not self._valid_signature(request.headers.get("X-Hub-Signature-256"), body):
            raise web.HTTPUnauthorized()

        delivery_guid = request.headers.get("X-GitHub-Delivery")
        event = request.headers.get("X-GitHub-Event")
        if not delivery_guid or not event:
            raise web.HTTPBadRequest()

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise web.HTTPBadRequest() from None
        if not isinstance(payload, Mapping):
            raise web.HTTPBadRequest()

        installation_id, repository_id, pr_number, action = self._delivery_identity(
            payload,
            event,
        )
        try:
            await self._store.accept_delivery(
                delivery_guid=delivery_guid,
                github_delivery_id=None,
                event=event,
                action=action,
                installation_id=installation_id,
                repository_id=repository_id,
                pr_number=pr_number,
                received_at=datetime.now(timezone.utc),
                raw_body=body,
            )
        except ValueError:
            raise web.HTTPBadRequest() from None
        except Exception:
            raise web.HTTPServiceUnavailable() from None
        return web.Response(status=202)

    def _delivery_identity(
        self,
        payload: Mapping[str, object],
        event: str,
    ) -> tuple[int, int | None, int | None, str | None]:
        installation_id = _nested_integer(payload, "installation", "id")
        if event == "ping" and installation_id is None:
            installation_id = self._credentials.installation_id
        organization = _nested_string(payload, "organization", "login")
        if (
            installation_id != self._credentials.installation_id
            or organization is None
            or organization.casefold() != self._organization
        ):
            raise web.HTTPForbidden()

        repository_id = _nested_integer(payload, "repository", "id")
        repository_name = _nested_string(payload, "repository", "full_name")
        repository_owner, separator, repository = (repository_name or "").partition("/")
        if repository_id is not None and (
            not separator
            or not repository
            or repository_owner.casefold() != self._organization
        ):
            raise web.HTTPForbidden()

        pr_number = _nested_integer(payload, "pull_request", "number")
        if event in _PULL_REQUEST_EVENTS:
            if repository_id is None or pr_number is None:
                raise web.HTTPBadRequest()
        else:
            repository_id = None
            pr_number = None
        action = payload.get("action")
        return (
            installation_id,
            repository_id,
            pr_number,
            action if isinstance(action, str) else None,
        )

    def _valid_signature(self, provided: str | None, body: bytes) -> bool:
        if provided is None:
            return False
        expected = "sha256=" + hmac.new(
            self._credentials.webhook_secret,
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(provided, expected)


def _nested_integer(payload: Mapping[str, object], key: str, nested: str) -> int | None:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        return None
    nested_value = value.get(nested)
    if isinstance(nested_value, bool) or not isinstance(nested_value, int):
        return None
    return nested_value


def _nested_string(payload: Mapping[str, object], key: str, nested: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        return None
    nested_value = value.get(nested)
    return nested_value if isinstance(nested_value, str) else None
