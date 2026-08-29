from __future__ import annotations

import importlib
import os
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

    async def test_inline_runtime_credentials_form_one_immutable_value(self) -> None:
        bot = _Bot(
            {
                "organization": "NewHorizons",
                "client_id": "Iv1.example",
                "app_id": "123",
                "installation_id": "456",
                "private_key": "private-key",
                "webhook_secret": "webhook-secret",
            }
        )

        loaded = await self.credentials.load_github_app_credentials(bot)

        self.assertEqual(bot.requested_services, ["githubtickets"])
        self.assertEqual(loaded.organization, "NewHorizons")
        self.assertEqual(loaded.client_id, "Iv1.example")
        self.assertEqual(loaded.app_id, 123)
        self.assertEqual(loaded.installation_id, 456)
        self.assertEqual(loaded.private_key, b"private-key")
        self.assertEqual(loaded.webhook_secret, b"webhook-secret")
        self.assertNotIn("private-key", repr(loaded))
        self.assertNotIn("webhook-secret", repr(loaded))

    async def test_secret_files_are_loaded_without_persisting_their_paths(self) -> None:
        root = Path(self.directory.name)
        private_key_path = root / "github-app.pem"
        webhook_secret_path = root / "webhook-secret.txt"
        private_key_path.write_bytes(b"private-key-file")
        webhook_secret_path.write_bytes(b"webhook-secret-file\n")
        bot = _Bot(
            {
                "organization": "NewHorizons",
                "client_id": "Iv1.example",
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": str(private_key_path),
                "webhook_secret_path": str(webhook_secret_path),
            }
        )

        loaded = await self.credentials.load_github_app_credentials(bot)

        self.assertEqual(loaded.private_key, b"private-key-file")
        self.assertEqual(loaded.webhook_secret, b"webhook-secret-file")
        self.assertNotIn(str(private_key_path), repr(loaded))
        self.assertNotIn(str(webhook_secret_path), repr(loaded))

    async def test_secret_file_paths_must_be_absolute(self) -> None:
        root = Path(self.directory.name)
        (root / "github-app.pem").write_bytes(b"private-key-file")
        (root / "webhook-secret.txt").write_bytes(b"webhook-secret-file")
        bot = _Bot(
            {
                "organization": "NewHorizons",
                "client_id": "Iv1.example",
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": "github-app.pem",
                "webhook_secret_path": "webhook-secret.txt",
            }
        )
        previous_directory = Path.cwd()
        os.chdir(root)
        try:
            with self.assertRaises(self.credentials.InvalidGitHubAppCredentials):
                await self.credentials.load_github_app_credentials(bot)
        finally:
            os.chdir(previous_directory)

    async def test_missing_is_dormant_and_partial_or_ambiguous_values_are_rejected(self) -> None:
        self.assertIsNone(await self.credentials.load_github_app_credentials(_Bot({})))

        for tokens in (
            {"organization": "NewHorizons"},
            {
                "organization": "NewHorizons",
                "client_id": "Iv1.example",
                "app_id": "123",
                "installation_id": "456",
                "private_key": "inline",
                "private_key_path": "also-a-path",
                "webhook_secret": "secret",
            },
        ):
            with self.subTest(tokens=tuple(tokens)):
                with self.assertRaises(self.credentials.InvalidGitHubAppCredentials):
                    await self.credentials.load_github_app_credentials(_Bot(tokens))


if __name__ == "__main__":
    unittest.main()
