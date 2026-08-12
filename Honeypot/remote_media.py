"""Bounded classification for remotely referenced media."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp.abc import AbstractResolver

MAX_WEBP_BYTES = 256 * 1024
WEBP_CONNECT_TIMEOUT_SECONDS = 2
WEBP_TOTAL_TIMEOUT_SECONDS = 4
WEBP_DNS_TIMEOUT_SECONDS = 2
WEBP_MAX_CONCURRENCY = 2
WEBP_CACHE_TTL_SECONDS = 300
WEBP_FAILURE_CACHE_TTL_SECONDS = 30
WEBP_CACHE_LIMIT = 512
RIFF_HEADER_SIZE = 12
VP8X_PAYLOAD_SIZE = 10
ANIM_PAYLOAD_SIZE = 6
ANMF_MIN_PAYLOAD_SIZE = 16
VP8_FRAME_HEADER_SIZE = 10
VP8L_FRAME_HEADER_SIZE = 5
VP8L_SIGNATURE = 0x2F
MIN_ANIMATION_FRAMES = 2


def _vp8_frame_dimensions(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < VP8_FRAME_HEADER_SIZE:
        return None
    frame_tag = int.from_bytes(payload[:3], "little")
    width = int.from_bytes(payload[6:8], "little") & 0x3FFF
    height = int.from_bytes(payload[8:10], "little") & 0x3FFF
    if frame_tag & 1 or payload[3:6] != b"\x9d\x01\x2a" or width == 0 or height == 0:
        return None
    return width, height


def _vp8l_frame_dimensions(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < VP8L_FRAME_HEADER_SIZE or payload[0] != VP8L_SIGNATURE:
        return None
    header = int.from_bytes(payload[1:5], "little")
    if header >> 29 != 0:
        return None
    return (header & 0x3FFF) + 1, ((header >> 14) & 0x3FFF) + 1


def _frame_dimensions(payload: bytes) -> tuple[int, int] | None:  # noqa: PLR0911
    """Require a syntactically valid VP8/VP8L image after the ANMF header."""

    if len(payload) < ANMF_MIN_PAYLOAD_SIZE or payload[15] & 0xFC:
        return None

    offset = ANMF_MIN_PAYLOAD_SIZE
    while offset < len(payload):
        if offset + 8 > len(payload):
            return None
        kind = payload[offset : offset + 4]
        size = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        payload_end = offset + 8 + size
        next_offset = payload_end + (size % 2)
        if payload_end > len(payload) or next_offset > len(payload):
            return None
        if kind in (b"VP8 ", b"VP8L"):
            image = payload[offset + 8 : payload_end]
            dimensions = (
                _vp8_frame_dimensions(image)
                if kind == b"VP8 "
                else _vp8l_frame_dimensions(image)
            )
            if payload_end != len(payload) and next_offset != len(payload):
                return None
            return dimensions
        if kind != b"ALPH":
            return None
        offset = next_offset
    return None


def _uint24(payload: bytes) -> int:
    return int.from_bytes(payload, "little")


def _valid_animation_frame(payload: bytes, canvas: tuple[int, int]) -> bool:
    image_dimensions = _frame_dimensions(payload)
    if image_dimensions is None:
        return False
    x = _uint24(payload[0:3]) * 2
    y = _uint24(payload[3:6]) * 2
    width = _uint24(payload[6:9]) + 1
    height = _uint24(payload[9:12]) + 1
    canvas_width, canvas_height = canvas
    return (
        image_dimensions == (width, height)
        and x + width <= canvas_width
        and y + height <= canvas_height
    )


async def _resolve_public_addresses(host: str) -> list[tuple[int, str]]:
    loop = asyncio.get_running_loop()
    answers = await loop.getaddrinfo(
        host,
        443,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    resolved: list[tuple[int, str]] = []
    for family, _type, _proto, _canonname, sockaddr in answers:
        item = (family, sockaddr[0])
        if item not in resolved:
            resolved.append(item)
    return resolved


class _PinnedResolver(AbstractResolver):
    def __init__(self, hostname: str, addresses: Sequence[tuple[int, str]]) -> None:
        self._hostname = hostname.casefold()
        self._addresses = tuple(addresses)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
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


def _validated_webp_url(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(value)
        port = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.casefold() != "https"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not hostname
    ):
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return value, hostname
    return None


class RemoteMediaInspector:
    """Prove animation for a direct WebP URL through a pinned bounded request."""

    def __init__(
        self,
        *,
        resolve_host: Callable[[str], Awaitable[list[tuple[int, str]]]] | None = None,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
    ) -> None:
        self._resolve_host = resolve_host or _resolve_public_addresses
        self._session_factory = session_factory
        self._semaphore = asyncio.Semaphore(WEBP_MAX_CONCURRENCY)
        self._cache: dict[str, tuple[float, bool | None]] = {}

    async def inspect(self, url: str) -> bool | None:
        validated = _validated_webp_url(url)
        if validated is None:
            return None
        request_url, hostname = validated
        cached = self._cache.get(request_url)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            async with self._semaphore:
                addresses = await asyncio.wait_for(
                    self._resolve_host(hostname),
                    timeout=WEBP_DNS_TIMEOUT_SECONDS,
                )
                if not addresses or any(
                    not ipaddress.ip_address(address).is_global
                    for _family, address in addresses
                ):
                    self._remember(request_url, None)
                    return None
                result = await self._fetch_and_classify(
                    request_url,
                    hostname,
                    addresses,
                )
                self._remember(request_url, result)
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            self._remember(request_url, None)
            return None

    async def _fetch_and_classify(
        self,
        url: str,
        hostname: str,
        addresses: Sequence[tuple[int, str]],
    ) -> bool | None:
        connector = aiohttp.TCPConnector(
            resolver=_PinnedResolver(hostname, addresses),
            use_dns_cache=False,
            force_close=True,
            limit=1,
        )
        timeout = aiohttp.ClientTimeout(
            total=WEBP_TOTAL_TIMEOUT_SECONDS,
            connect=WEBP_CONNECT_TIMEOUT_SECONDS,
        )
        async with self._session_factory(
            connector=connector,
            timeout=timeout,
            trust_env=False,
            auto_decompress=False,
        ) as session, session.get(
            url,
            headers={
                "Accept": "image/webp",
                "Accept-Encoding": "identity",
                "Range": f"bytes=0-{MAX_WEBP_BYTES - 1}",
            },
            allow_redirects=False,
        ) as response:
            if response.status not in (200, 206):
                return None
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_WEBP_BYTES:
                    return None
            return classify_webp_animation(bytes(data))

    def _remember(self, url: str, result: bool | None) -> None:
        ttl = (
            WEBP_CACHE_TTL_SECONDS
            if result is not None
            else WEBP_FAILURE_CACHE_TTL_SECONDS
        )
        self._cache[url] = (time.monotonic() + ttl, result)
        if len(self._cache) > WEBP_CACHE_LIMIT:
            oldest = next(iter(self._cache))
            del self._cache[oldest]


def classify_webp_animation(data: bytes) -> bool | None:  # noqa: PLR0911, PLR0912
    """Return True for animated WebP, False for complete static WebP, else None."""

    if (
        len(data) < RIFF_HEADER_SIZE
        or data[:4] != b"RIFF"
        or data[8:12] != b"WEBP"
    ):
        return None
    riff_size = int.from_bytes(data[4:8], "little")
    expected_size = riff_size + 8
    if expected_size != len(data):
        return None

    offset = RIFF_HEADER_SIZE
    animation_header = False
    animation_control = False
    animation_frames = 0
    canvas: tuple[int, int] | None = None
    first_chunk = True
    while offset < expected_size:
        if offset + 8 > len(data):
            return None
        kind = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload_start = offset + 8
        payload_end = payload_start + size
        next_offset = payload_end + (size % 2)
        if payload_end > len(data) or next_offset > len(data):
            return None

        if kind == b"VP8X":
            if (
                not first_chunk
                or size != VP8X_PAYLOAD_SIZE
                or data[payload_start] & 0xC1
                or any(data[payload_start + 1 : payload_start + 4])
            ):
                return None
            animation_header = bool(data[payload_start] & 0b00000010)
            canvas = (
                _uint24(data[payload_start + 4 : payload_start + 7]) + 1,
                _uint24(data[payload_start + 7 : payload_start + 10]) + 1,
            )
        elif kind == b"ANIM":
            if (
                not animation_header
                or animation_control
                or size != ANIM_PAYLOAD_SIZE
            ):
                return None
            animation_control = True
        elif kind == b"ANMF":
            if (
                not animation_control
                or canvas is None
                or not _valid_animation_frame(
                    data[payload_start:payload_end], canvas
                )
            ):
                return None
            animation_frames += 1
        elif kind in (b"VP8 ", b"VP8L") and animation_header:
            return None
        first_chunk = False
        offset = next_offset

    if offset != expected_size:
        return None
    if animation_header:
        return (
            True
            if animation_control and animation_frames >= MIN_ANIMATION_FRAMES
            else None
        )
    return False
