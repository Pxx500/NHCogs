from __future__ import annotations

import asyncio
import unittest
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.githubtickets_loader import isolated_githubtickets_modules


class FakeResponse:
    def __init__(
        self,
        status: int,
        payload: dict[str, object] | list[object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def json(self, *, content_type=None):
        return self._payload


class FakeSession:
    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        self.responses = deque(responses)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


class DeferredResponse(FakeResponse):
    def __init__(self, status: int, payload: dict[str, object]) -> None:
        super().__init__(status, payload)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def json(self, *, content_type=None):
        self.entered.set()
        await self.release.wait()
        return await super().json(content_type=content_type)


class GitHubAppClientTests(unittest.IsolatedAsyncioTestCase):
    private_key: rsa.RSAPrivateKey
    private_pem: bytes
    public_key: rsa.RSAPublicKey

    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_pem = cls.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        cls.public_key = cls.private_key.public_key()

    def setUp(self) -> None:
        self.data_dir = TemporaryDirectory()
        self.modules = isolated_githubtickets_modules(Path(self.data_dir.name))
        self.loaded = self.modules.__enter__()

    def tearDown(self) -> None:
        self.modules.__exit__(None, None, None)
        self.data_dir.cleanup()

    def credentials(self):
        return self.loaded.github_app.GitHubAppCredentials(
            client_id="Iv1.client",
            app_id=123,
            installation_id=456,
            private_key=self.private_pem,
            webhook_secret=b"webhook-secret",
        )

    @staticmethod
    def pull_request_payload() -> dict[str, object]:
        return {
            "id": 9001,
            "number": 42,
            "title": "Make the machine faster",
            "html_url": "https://github.com/GTNewHorizons/Example/pull/42",
            "state": "open",
            "draft": False,
            "merged": False,
            "user": {"login": "author"},
            "labels": [{"name": "discord-ticket"}],
            "assignees": [{"login": "reviewer"}],
        }

    async def test_first_pr_read_authenticates_and_returns_snapshot(self) -> None:
        github_app = self.loaded.github_app
        session = FakeSession(
            FakeResponse(
                201,
                {
                    "token": "installation-token",
                    "expires_at": "2026-08-29T01:00:00Z",
                },
            ),
            FakeResponse(200, self.pull_request_payload()),
        )
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        credentials = self.credentials()
        client = github_app.GitHubAppClient(credentials, session, clock=lambda: now)

        pull_request = await client.get_pull_request("GTNewHorizons", "Example", 42)

        self.assertEqual(pull_request.node_id, 9001)
        self.assertEqual(pull_request.number, 42)
        self.assertEqual(pull_request.title, "Make the machine faster")
        self.assertEqual(pull_request.author_login, "author")
        self.assertEqual(pull_request.labels, ("discord-ticket",))
        self.assertEqual(pull_request.assignees, ("reviewer",))

        token_request, pull_request_request = session.requests
        self.assertEqual(token_request[0:2], ("POST", "https://api.github.com/app/installations/456/access_tokens"))
        encoded_jwt = token_request[2]["headers"]["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            encoded_jwt,
            self.public_key,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        self.assertEqual(claims, {"iat": int(now.timestamp()) - 60, "exp": int(now.timestamp()) + 540, "iss": "Iv1.client"})
        self.assertEqual(
            pull_request_request[2]["headers"],
            {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer installation-token",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "NHCogs-GitHubTickets",
            },
        )
        self.assertNotIn("webhook-secret", repr(credentials))
        self.assertNotIn(self.private_pem.decode(), repr(credentials))

    async def test_unauthorized_request_refreshes_token_once(self) -> None:
        github_app = self.loaded.github_app
        session = FakeSession(
            FakeResponse(
                201,
                {"token": "expired-token", "expires_at": "2026-08-29T01:00:00Z"},
            ),
            FakeResponse(401, {"message": "Bad credentials"}),
            FakeResponse(
                201,
                {"token": "fresh-token", "expires_at": "2026-08-29T01:00:00Z"},
            ),
            FakeResponse(200, self.pull_request_payload()),
        )
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        client = github_app.GitHubAppClient(self.credentials(), session, clock=lambda: now)

        pull_request = await client.get_pull_request("GTNewHorizons", "Example", 42)

        self.assertEqual(pull_request.number, 42)
        token_requests = [request for request in session.requests if "/access_tokens" in request[1]]
        self.assertEqual(len(token_requests), 2)
        self.assertEqual(session.requests[-1][2]["headers"]["Authorization"], "Bearer fresh-token")

    async def test_add_assignee_rejects_silent_github_ignore(self) -> None:
        github_app = self.loaded.github_app
        session = FakeSession(
            FakeResponse(
                201,
                {"token": "installation-token", "expires_at": "2026-08-29T01:00:00Z"},
            ),
            FakeResponse(200, {"assignees": [{"login": "someone-else"}]}),
        )
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        client = github_app.GitHubAppClient(self.credentials(), session, clock=lambda: now)

        with self.assertRaises(github_app.GitHubAssigneeUnavailable) as raised:
            await client.add_assignee("GTNewHorizons", "Example", 42, "reviewer")

        self.assertEqual(raised.exception.login, "reviewer")
        request = session.requests[-1]
        self.assertEqual(request[0:2], ("POST", "https://api.github.com/repos/GTNewHorizons/Example/issues/42/assignees"))
        self.assertEqual(request[2]["json"], {"assignees": ["reviewer"]})

    async def test_remove_assignee_uses_pull_request_issue_endpoint(self) -> None:
        github_app = self.loaded.github_app
        session = FakeSession(
            FakeResponse(
                201,
                {"token": "installation-token", "expires_at": "2026-08-29T01:00:00Z"},
            ),
            FakeResponse(200, {"assignees": []}),
        )
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        client = github_app.GitHubAppClient(self.credentials(), session, clock=lambda: now)

        await client.remove_assignee("GTNewHorizons", "Example", 42, "reviewer")

        request = session.requests[-1]
        self.assertEqual(request[0:2], ("DELETE", "https://api.github.com/repos/GTNewHorizons/Example/issues/42/assignees"))
        self.assertEqual(request[2]["json"], {"assignees": ["reviewer"]})

    async def test_app_delivery_operations_use_app_jwt(self) -> None:
        github_app = self.loaded.github_app
        session = FakeSession(
            FakeResponse(
                200,
                [
                    {
                        "id": 765,
                        "guid": "delivery-guid",
                        "delivered_at": "2026-08-29T00:10:00Z",
                        "redelivery": False,
                        "status_code": 503,
                        "event": "pull_request",
                        "action": "closed",
                    }
                ],
            ),
            FakeResponse(202, {}),
        )
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        client = github_app.GitHubAppClient(self.credentials(), session, clock=lambda: now)

        deliveries = await client.list_deliveries(page=2)
        await client.redeliver(765)

        self.assertEqual(
            deliveries,
            (
                github_app.GitHubDeliverySummary(
                    delivery_id=765,
                    guid="delivery-guid",
                    delivered_at=datetime(2026, 8, 29, 0, 10, tzinfo=timezone.utc),
                    redelivery=False,
                    status_code=503,
                    event="pull_request",
                    action="closed",
                ),
            ),
        )
        self.assertEqual(
            [request[0:2] for request in session.requests],
            [
                ("GET", "https://api.github.com/app/hook/deliveries?per_page=100&page=2"),
                ("POST", "https://api.github.com/app/hook/deliveries/765/attempts"),
            ],
        )
        for request in session.requests:
            encoded_jwt = request[2]["headers"]["Authorization"].removeprefix("Bearer ")
            claims = jwt.decode(
                encoded_jwt,
                self.public_key,
                algorithms=["RS256"],
                options={"verify_exp": False, "verify_iat": False},
            )
            self.assertEqual(claims["iss"], "Iv1.client")

    async def test_rate_limit_returns_safe_structured_failure(self) -> None:
        github_app = self.loaded.github_app
        session = FakeSession(
            FakeResponse(
                201,
                {"token": "installation-token", "expires_at": "2026-08-29T01:00:00Z"},
            ),
            FakeResponse(
                403,
                {"message": "sensitive upstream response body"},
                headers={"Retry-After": "120"},
            ),
        )
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        client = github_app.GitHubAppClient(self.credentials(), session, clock=lambda: now)

        with self.assertRaises(github_app.GitHubRequestError) as raised:
            await client.get_pull_request("GTNewHorizons", "Example", 42)

        error = raised.exception
        self.assertEqual(error.status, 403)
        self.assertTrue(error.retryable)
        self.assertTrue(error.rate_limited)
        self.assertEqual(error.retry_at, datetime(2026, 8, 29, 0, 2, tzinfo=timezone.utc))
        self.assertNotIn("sensitive upstream", str(error))
        self.assertLessEqual(len(str(error)), 120)

    async def test_network_failure_is_safe_and_retryable(self) -> None:
        github_app = self.loaded.github_app
        session = FakeSession(OSError("socket failed with sensitive local detail"))
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        client = github_app.GitHubAppClient(self.credentials(), session, clock=lambda: now)

        with self.assertRaises(github_app.GitHubRequestError) as raised:
            await client.get_pull_request("GTNewHorizons", "Example", 42)

        error = raised.exception
        self.assertIsNone(error.status)
        self.assertTrue(error.retryable)
        self.assertFalse(error.rate_limited)
        self.assertNotIn("sensitive local detail", str(error))

    async def test_concurrent_requests_coalesce_token_refresh(self) -> None:
        github_app = self.loaded.github_app
        token_response = DeferredResponse(
            201,
            {"token": "installation-token", "expires_at": "2026-08-29T01:00:00Z"},
        )
        session = FakeSession(
            token_response,
            FakeResponse(200, self.pull_request_payload()),
            FakeResponse(200, self.pull_request_payload()),
        )
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        client = github_app.GitHubAppClient(self.credentials(), session, clock=lambda: now)

        first = asyncio.create_task(client.get_pull_request("GTNewHorizons", "Example", 42))
        await token_response.entered.wait()
        second = asyncio.create_task(client.get_pull_request("GTNewHorizons", "Example", 42))
        await asyncio.sleep(0)

        token_requests = [request for request in session.requests if "/access_tokens" in request[1]]
        self.assertEqual(len(token_requests), 1)
        token_response.release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual([result.number for result in results], [42, 42])


if __name__ == "__main__":
    unittest.main()
