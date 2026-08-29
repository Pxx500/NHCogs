from __future__ import annotations

import hashlib
import hmac
import json
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from tests.githubtickets_loader import isolated_githubtickets_modules


class GitHubWebhookReceiverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.data_dir = TemporaryDirectory()
        self.modules = isolated_githubtickets_modules(Path(self.data_dir.name))
        self.loaded = self.modules.__enter__()

    async def asyncSetUp(self) -> None:
        self.store = self.loaded.store.GitHubTicketsStore(
            Path(self.data_dir.name) / "githubtickets.sqlite"
        )
        await self.store.initialize()
        self.credentials = self.loaded.github_app.GitHubAppCredentials(
            client_id="Iv1.client",
            app_id=123,
            installation_id=456,
            private_key=b"private-key",
            webhook_secret=b"webhook-secret",
        )
        self.receiver = self.loaded.webhook.GitHubWebhookReceiver(
            self.store,
            self.credentials,
            organization="GTNewHorizons",
        )
        self.client = TestClient(TestServer(self.receiver.application))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    def tearDown(self) -> None:
        self.modules.__exit__(None, None, None)
        self.data_dir.cleanup()

    @staticmethod
    def payload() -> dict[str, object]:
        return {
            "action": "labeled",
            "installation": {"id": 456},
            "organization": {"login": "GTNewHorizons"},
            "repository": {
                "id": 9001,
                "full_name": "GTNewHorizons/Example",
            },
            "pull_request": {"number": 42},
        }

    def signed_headers(
        self,
        body: bytes,
        *,
        delivery: str = "delivery-guid",
        event: str = "pull_request",
    ) -> dict[str, str]:
        signature = "sha256=" + hmac.new(
            self.credentials.webhook_secret,
            body,
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature,
        }

    async def test_valid_delivery_is_durable_before_success_response(self) -> None:
        body = json.dumps(self.payload(), separators=(",", ":")).encode()

        response = await self.client.post(
            self.loaded.webhook.WEBHOOK_PATH,
            data=body,
            headers=self.signed_headers(body),
        )

        self.assertEqual(response.status, 202)
        delivery = await self.store.get_delivery("delivery-guid")
        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.assertEqual(delivery.event, "pull_request")
        self.assertEqual(delivery.action, "labeled")
        self.assertEqual(delivery.installation_id, 456)
        self.assertEqual(delivery.repository_id, 9001)
        self.assertEqual(delivery.pr_number, 42)
        self.assertEqual(delivery.raw_body, body)

    async def test_documented_ping_without_installation_is_accepted(self) -> None:
        body = json.dumps(
            {
                "hook": {"type": "App"},
                "organization": {"login": "GTNewHorizons"},
                "repository": {
                    "id": 9001,
                    "full_name": "GTNewHorizons/Example",
                },
                "sender": {"login": "octocat"},
                "zen": "Keep it logically awesome",
            },
            separators=(",", ":"),
        ).encode()

        response = await self.client.post(
            self.loaded.webhook.WEBHOOK_PATH,
            data=body,
            headers=self.signed_headers(body, event="ping"),
        )

        self.assertEqual(response.status, 202)
        delivery = await self.store.get_delivery("delivery-guid")
        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.assertEqual(delivery.event, "ping")
        self.assertEqual(delivery.installation_id, 456)
        self.assertIsNone(delivery.repository_id)
        self.assertIsNone(delivery.pr_number)

    async def test_invalid_requests_are_rejected_without_persistence(self) -> None:
        cases: list[tuple[str, bytes, dict[str, str], int]] = []

        valid_body = json.dumps(self.payload(), separators=(",", ":")).encode()
        invalid_signature = self.signed_headers(valid_body)
        invalid_signature["X-Hub-Signature-256"] = "sha256=invalid"
        cases.append(("signature", valid_body, invalid_signature, 401))

        malformed_body = b"not-json"
        cases.append(
            ("payload", malformed_body, self.signed_headers(malformed_body), 400)
        )

        wrong_installation = self.payload()
        wrong_installation["installation"] = {"id": 999}
        wrong_installation_body = json.dumps(
            wrong_installation, separators=(",", ":")
        ).encode()
        cases.append(
            (
                "installation",
                wrong_installation_body,
                self.signed_headers(wrong_installation_body),
                403,
            )
        )

        wrong_organization = self.payload()
        wrong_organization["organization"] = {"login": "SomeoneElse"}
        wrong_organization_body = json.dumps(
            wrong_organization, separators=(",", ":")
        ).encode()
        cases.append(
            (
                "organization",
                wrong_organization_body,
                self.signed_headers(wrong_organization_body),
                403,
            )
        )

        missing_event_headers = self.signed_headers(valid_body)
        del missing_event_headers["X-GitHub-Event"]
        cases.append(("event", valid_body, missing_event_headers, 400))

        for name, body, headers, status in cases:
            with self.subTest(name=name):
                response = await self.client.post(
                    self.loaded.webhook.WEBHOOK_PATH,
                    data=body,
                    headers=headers,
                )
                self.assertEqual(response.status, status)

        self.assertIsNone(await self.store.get_delivery("delivery-guid"))

    async def test_signature_covers_the_exact_raw_json_bytes(self) -> None:
        compact_body = json.dumps(self.payload(), separators=(",", ":")).encode()
        formatted_body = json.dumps(self.payload(), indent=2).encode()

        response = await self.client.post(
            self.loaded.webhook.WEBHOOK_PATH,
            data=formatted_body,
            headers=self.signed_headers(compact_body),
        )

        self.assertEqual(response.status, 401)
        self.assertIsNone(await self.store.get_delivery("delivery-guid"))

    async def test_only_the_fixed_post_route_accepts_webhooks(self) -> None:
        get_response = await self.client.get(self.loaded.webhook.WEBHOOK_PATH)
        wrong_route_response = await self.client.post("/other")

        self.assertEqual(get_response.status, 405)
        self.assertEqual(wrong_route_response.status, 404)

    async def test_pull_request_delivery_requires_repository_and_pr_identity(self) -> None:
        payload = self.payload()
        del payload["repository"]
        body = json.dumps(payload, separators=(",", ":")).encode()

        response = await self.client.post(
            self.loaded.webhook.WEBHOOK_PATH,
            data=body,
            headers=self.signed_headers(body),
        )

        self.assertEqual(response.status, 400)
        self.assertIsNone(await self.store.get_delivery("delivery-guid"))

    async def test_duplicate_delivery_is_acknowledged_without_replacing_payload(self) -> None:
        first_body = json.dumps(self.payload(), separators=(",", ":")).encode()
        first_response = await self.client.post(
            self.loaded.webhook.WEBHOOK_PATH,
            data=first_body,
            headers=self.signed_headers(first_body),
        )

        duplicate_payload = self.payload()
        duplicate_payload["action"] = "edited"
        duplicate_body = json.dumps(
            duplicate_payload, separators=(",", ":")
        ).encode()
        duplicate_response = await self.client.post(
            self.loaded.webhook.WEBHOOK_PATH,
            data=duplicate_body,
            headers=self.signed_headers(duplicate_body),
        )

        self.assertEqual(first_response.status, 202)
        self.assertEqual(duplicate_response.status, 202)
        delivery = await self.store.get_delivery("delivery-guid")
        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.assertEqual(delivery.action, "labeled")
        self.assertEqual(delivery.raw_body, first_body)

    async def test_store_failure_is_not_acknowledged(self) -> None:
        body = json.dumps(self.payload(), separators=(",", ":")).encode()
        self.store.accept_delivery = AsyncMock(side_effect=OSError("database unavailable"))

        response = await self.client.post(
            self.loaded.webhook.WEBHOOK_PATH,
            data=body,
            headers=self.signed_headers(body),
        )

        self.assertEqual(response.status, 503)

    async def test_body_larger_than_limit_is_rejected(self) -> None:
        body = b"x" * (2 * 1024 * 1024)

        response = await self.client.post(
            self.loaded.webhook.WEBHOOK_PATH,
            data=body,
            headers=self.signed_headers(body),
        )

        self.assertEqual(response.status, 413)

    async def test_receiver_start_and_close_control_network_acceptance(self) -> None:
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]

        receiver = self.loaded.webhook.GitHubWebhookReceiver(
            self.store,
            self.credentials,
            organization="GTNewHorizons",
        )
        started_port = await receiver.start("127.0.0.1", port)
        self.assertEqual(started_port, port)

        async with aiohttp.ClientSession() as session:
            response = await session.post(
                f"http://127.0.0.1:{port}{self.loaded.webhook.WEBHOOK_PATH}"
            )
            self.assertEqual(response.status, 401)

            await receiver.close()
            with self.assertRaises(aiohttp.ClientConnectionError):
                await session.post(
                    f"http://127.0.0.1:{port}{self.loaded.webhook.WEBHOOK_PATH}"
                )

    async def test_bind_failure_cleans_up_for_a_later_start(self) -> None:
        receiver = self.loaded.webhook.GitHubWebhookReceiver(
            self.store,
            self.credentials,
            organization="GTNewHorizons",
        )
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            reserved.listen()
            port = reserved.getsockname()[1]
            with self.assertRaises(OSError):
                await receiver.start("127.0.0.1", port)

        self.assertEqual(await receiver.start("127.0.0.1", port), port)
        await receiver.close()
