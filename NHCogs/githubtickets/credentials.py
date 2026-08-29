from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .github_app import GitHubAppCredentials

_TOKEN_SERVICE = "githubtickets"
_MAX_SECRET_BYTES = 1024 * 1024


class InvalidGitHubAppCredentials(ValueError):
    pass


async def load_github_app_credentials(bot: Any) -> GitHubAppCredentials | None:
    raw = await bot.get_shared_api_tokens(_TOKEN_SERVICE)
    if not raw:
        return None
    if not isinstance(raw, Mapping):
        raise InvalidGitHubAppCredentials("GitHub App credentials are invalid")
    organization = _required_text(raw, "organization")
    client_id = _required_text(raw, "client_id")
    app_id = _positive_integer(raw, "app_id")
    installation_id = _positive_integer(raw, "installation_id")
    private_key = await _secret(raw, "private_key")
    webhook_secret = await _secret(raw, "webhook_secret")
    return GitHubAppCredentials(
        organization=organization,
        client_id=client_id,
        app_id=app_id,
        installation_id=installation_id,
        private_key=private_key,
        webhook_secret=webhook_secret,
    )


def _required_text(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidGitHubAppCredentials("GitHub App credentials are incomplete")
    return value.strip()


def _positive_integer(values: Mapping[str, object], name: str) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InvalidGitHubAppCredentials("GitHub App credentials are incomplete")
    try:
        parsed = int(value)
    except ValueError:
        raise InvalidGitHubAppCredentials("GitHub App credentials are invalid") from None
    if parsed <= 0:
        raise InvalidGitHubAppCredentials("GitHub App credentials are invalid")
    return parsed


async def _secret(values: Mapping[str, object], name: str) -> bytes:
    inline = values.get(name)
    path = values.get(f"{name}_path")
    has_inline = isinstance(inline, str) and bool(inline.strip())
    has_path = isinstance(path, str) and bool(path.strip())
    if has_inline == has_path:
        raise InvalidGitHubAppCredentials("GitHub App credentials are incomplete")
    if has_inline and isinstance(inline, str):
        return inline.strip().encode()
    if not isinstance(path, str):
        raise InvalidGitHubAppCredentials("GitHub App credentials are incomplete")
    secret_path = Path(path.strip())
    if not secret_path.is_absolute():
        raise InvalidGitHubAppCredentials("GitHub App secret file is unavailable")
    try:
        secret = await asyncio.to_thread(_read_secret_file, secret_path)
    except (OSError, ValueError):
        raise InvalidGitHubAppCredentials("GitHub App secret file is unavailable") from None
    return secret


def _read_secret_file(path: Path) -> bytes:
    if path.stat().st_size > _MAX_SECRET_BYTES:
        raise ValueError("secret file is too large")
    value = path.read_bytes()
    if len(value) > _MAX_SECRET_BYTES:
        raise ValueError("secret file is too large")
    value = value.strip()
    if not value:
        raise ValueError("secret file is empty")
    return value
