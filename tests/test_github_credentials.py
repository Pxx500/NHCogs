from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.githubtickets_loader import isolated_githubtickets_modules


class _Bot:
    def __init__(self, tokens: dict[str, str]) -> None:
        self.tokens = tokens
        self.requested_services: list[str] = []

    async def get_shared_api_tokens(self, service: str) -> dict[str, str]:
        self.requested_services.append(service)
        return self.tokens


class GitHubCredentialsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.modules_context = isolated_githubtickets_modules(Path(self.directory.name))
        self.modules_context.__enter__()
        self.module_name = "NHCogs.githubtickets.credentials"
        self.previous_module = sys.modules.pop(self.module_name, None)
        self.credentials = importlib.import_module(self.module_name)

    async def asyncTearDown(self) -> None:
        sys.modules.pop(self.module_name, None)
        if self.previous_module is not None:
            sys.modules[self.module_name] = self.previous_module
        self.modules_context.__exit__(None, None, None)
        self.directory.cleanup()

    async def test_fixed_secret_files_form_one_immutable_value(self) -> None:
        data_path = Path(self.directory.name)
        secret_path = data_path / "secrets"
        secret_path.mkdir()
        (secret_path / "github-app.pem").write_bytes(b"private-key")
        (secret_path / "webhook-secret.txt").write_bytes(b"webhook-secret\n")
        bot = _Bot(
            {
                "organization": "NewHorizons",
                "client_id": "Iv1.example",
                "app_id": "123",
                "installation_id": "456",
            }
        )

        loaded = await self.credentials.load_github_app_credentials(bot, data_path)

        self.assertEqual(bot.requested_services, ["githubtickets"])
        self.assertEqual(loaded.organization, "NewHorizons")
        self.assertEqual(loaded.client_id, "Iv1.example")
        self.assertEqual(loaded.app_id, 123)
        self.assertEqual(loaded.installation_id, 456)
        self.assertEqual(loaded.private_key, b"private-key")
        self.assertEqual(loaded.webhook_secret, b"webhook-secret")
        self.assertNotIn("private-key", repr(loaded))
        self.assertNotIn("webhook-secret", repr(loaded))

    async def test_missing_fixed_secret_files_are_rejected(self) -> None:
        data_path = Path(self.directory.name)
        bot = _Bot(
            {
                "organization": "NewHorizons",
                "client_id": "Iv1.example",
                "app_id": "123",
                "installation_id": "456",
            }
        )

        with self.assertRaises(self.credentials.InvalidGitHubAppCredentials):
            await self.credentials.load_github_app_credentials(bot, data_path)

    async def test_missing_is_dormant_and_partial_metadata_is_rejected(self) -> None:
        data_path = Path(self.directory.name)
        self.assertIsNone(
            await self.credentials.load_github_app_credentials(_Bot({}), data_path)
        )

        with self.assertRaises(self.credentials.InvalidGitHubAppCredentials):
            await self.credentials.load_github_app_credentials(
                _Bot({"organization": "NewHorizons"}),
                data_path,
            )


if __name__ == "__main__":
    unittest.main()
