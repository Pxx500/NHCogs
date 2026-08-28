from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

ALLOWED_AVATAR_MEDIA_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
MAX_AVATAR_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 5
_HTTP_SUCCESS_MIN = 200
_HTTP_REDIRECT_MIN = 300
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class AttachmentLike(Protocol):
    size: int | None
    content_type: str | None

    async def read(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class LoadedAvatar:
    avatar_bytes: bytes
    avatar_media_type: str
    avatar_sha256: str


def _normalize_media_type(value: object) -> str:
    if value is None:
        raise ValueError("avatar media type is missing")
    if not isinstance(value, str):
        raise TypeError("avatar media type must be text")
    media_type = value.partition(";")[0].strip().casefold()
    if media_type not in ALLOWED_AVATAR_MEDIA_TYPES:
        raise ValueError("unsupported avatar media type")
    return media_type


def _loaded_avatar(data: bytes, media_type: str) -> LoadedAvatar:
    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError("avatar cannot exceed 2 MiB")
    return LoadedAvatar(
        avatar_bytes=data,
        avatar_media_type=media_type,
        avatar_sha256=hashlib.sha256(data).hexdigest(),
    )


def _url_destination(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError as error:
        raise ValueError("invalid avatar URL") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
    ):
        raise ValueError("avatar URL must be HTTPS without credentials")
    return parsed.hostname, 443 if parsed_port is None else parsed_port


async def _resolve_public_destination(
    hostname: str, port: int
) -> tuple[tuple[int, str], ...]:
    try:
        parsed = ipaddress.ip_address(hostname)
        family = (
            socket.AF_INET6
            if isinstance(parsed, ipaddress.IPv6Address)
            else socket.AF_INET
        )
        addresses = [(family, hostname)]
    except ValueError:
        answers = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        addresses = []
        for answer in answers:
            item = (answer[0], str(answer[4][0]))
            if item not in addresses:
                addresses.append(item)
    if not addresses or any(
        not (address := ipaddress.ip_address(value)).is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for _family, value in addresses
    ):
        raise ValueError("avatar URL must resolve to a public network address")
    return tuple(addresses)


class _PinnedResolver(AbstractResolver):
    def __init__(self, hostname: str, addresses: Sequence[tuple[int, str]]) -> None:
        self._hostname = hostname.casefold()
        self._addresses = tuple(addresses)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[Any]:
        if host.casefold() != self._hostname:
            raise OSError("resolver was asked for an unvalidated hostname")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": 0,
                "flags": 0,
            }
            for address_family, address in self._addresses
            if family in (socket.AF_UNSPEC, address_family)
        ]

    async def close(self) -> None:
        return None


class AvatarLoader:
    def __init__(
        self,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
    ) -> None:
        self._session_factory = session_factory

    async def from_url(self, url: str) -> LoadedAvatar:
        current_url = url
        for redirect_count in range(_MAX_REDIRECTS + 1):
            hostname, port = _url_destination(current_url)
            addresses = await _resolve_public_destination(hostname, port)
            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(hostname, addresses),
                use_dns_cache=False,
                force_close=True,
                limit=1,
            )
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            async with self._session_factory(
                connector=connector,
                timeout=timeout,
                trust_env=False,
                auto_decompress=False,
            ) as session, session.get(
                current_url,
                allow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                if response.status in _REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location or redirect_count == _MAX_REDIRECTS:
                        raise ValueError("avatar download has too many redirects")
                    current_url = urljoin(current_url, location)
                    continue
                if not _HTTP_SUCCESS_MIN <= response.status < _HTTP_REDIRECT_MIN:
                    raise ValueError("avatar download failed")
                media_type = _normalize_media_type(response.headers.get("Content-Type"))
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_AVATAR_BYTES:
                        raise ValueError("avatar cannot exceed 2 MiB")
                return _loaded_avatar(bytes(data), media_type)
        raise AssertionError("unreachable")

    async def from_attachment(self, source: AttachmentLike) -> LoadedAvatar:
        read = getattr(source, "read", None)
        if not callable(read):
            raise TypeError("avatar attachment cannot be read")
        advertised_size = getattr(source, "size", None)
        if isinstance(advertised_size, int) and advertised_size > MAX_AVATAR_BYTES:
            raise ValueError("avatar cannot exceed 2 MiB")
        media_type = _normalize_media_type(getattr(source, "content_type", None))
        data = bytes(await read())
        return _loaded_avatar(data, media_type)


async def load_avatar(
    session: aiohttp.ClientSession,
    source: str | AttachmentLike,
) -> LoadedAvatar:
    if isinstance(source, str):
        raise TypeError("use AvatarLoader.from_url for URL avatars")
    return await AvatarLoader().from_attachment(source)
