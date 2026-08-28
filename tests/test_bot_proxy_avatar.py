from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = (
    Path(__file__).parents[1] / "NHCogs" / "nhmisc" / "bot_proxy_avatar.py"
)
SPEC = importlib.util.spec_from_file_location("bot_proxy_avatar_test_module", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Bot Proxy avatar module")
bot_proxy_avatar = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bot_proxy_avatar
SPEC.loader.exec_module(bot_proxy_avatar)


class FakeContent:
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(*chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None


class FakeSessionContext:
    def __init__(self, session, connector) -> None:
        self.session = session
        self.connector = connector

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args) -> None:
        await self.connector.close()


def avatar_loader(session):
    def session_factory(**kwargs):
        return FakeSessionContext(session, kwargs["connector"])

    return bot_proxy_avatar.AvatarLoader(session_factory)


class BotProxyAvatarTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_is_copied_normalized_and_hashed(self) -> None:
        source_bytes = bytearray(b"avatar-bytes")
        attachment = SimpleNamespace(
            size=len(source_bytes),
            content_type="Image/PNG; charset=binary",
            read=mock.AsyncMock(return_value=source_bytes),
        )
        session = SimpleNamespace(get=mock.Mock())

        loaded = await bot_proxy_avatar.load_avatar(session, attachment)

        self.assertIsInstance(loaded.avatar_bytes, bytes)
        self.assertEqual(loaded.avatar_bytes, bytes(source_bytes))
        self.assertEqual(loaded.avatar_media_type, "image/png")
        self.assertEqual(
            loaded.avatar_sha256,
            hashlib.sha256(source_bytes).hexdigest(),
        )
        session.get.assert_not_called()

    async def test_oversized_attachment_is_rejected_before_read(self) -> None:
        attachment = SimpleNamespace(
            size=2 * 1024 * 1024 + 1,
            content_type="image/png",
            read=mock.AsyncMock(),
        )

        with self.assertRaisesRegex(ValueError, "2 MiB"):
            await bot_proxy_avatar.load_avatar(SimpleNamespace(), attachment)

        attachment.read.assert_not_awaited()

    async def test_attachment_bytes_are_bounded_when_size_is_unknown(self) -> None:
        attachment = SimpleNamespace(
            size=None,
            content_type="image/webp",
            read=mock.AsyncMock(return_value=b"x" * (2 * 1024 * 1024 + 1)),
        )

        with self.assertRaisesRegex(ValueError, "2 MiB"):
            await bot_proxy_avatar.load_avatar(SimpleNamespace(), attachment)

    async def test_https_avatar_is_resolved_streamed_normalized_and_hashed(self) -> None:
        response = FakeResponse(
            headers={"Content-Type": "IMAGE/JPEG; charset=binary"},
            chunks=(b"avatar-", b"bytes"),
        )
        session = SimpleNamespace(get=mock.Mock(return_value=response))
        loop = asyncio.get_running_loop()
        dns_answer = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            )
        ]

        with mock.patch.object(
            loop,
            "getaddrinfo",
            new=mock.AsyncMock(return_value=dns_answer),
        ) as resolve:
            loaded = await avatar_loader(session).from_url(
                "https://images.example/avatar.jpg",
            )

        resolve.assert_awaited_once_with(
            "images.example",
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        session.get.assert_called_once_with(
            "https://images.example/avatar.jpg",
            allow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        )
        self.assertEqual(loaded.avatar_bytes, b"avatar-bytes")
        self.assertEqual(loaded.avatar_media_type, "image/jpeg")
        self.assertEqual(
            loaded.avatar_sha256,
            hashlib.sha256(b"avatar-bytes").hexdigest(),
        )

    async def test_every_redirect_target_is_resolved_before_request(self) -> None:
        redirect = FakeResponse(
            status=302,
            headers={"Location": "https://cdn.example/avatar.webp"},
        )
        avatar = FakeResponse(
            headers={"Content-Type": "image/webp"},
            chunks=(b"webp-avatar",),
        )
        session = SimpleNamespace(
            get=mock.Mock(side_effect=[redirect, avatar]),
        )
        loop = asyncio.get_running_loop()
        resolve = mock.AsyncMock(
            side_effect=[
                [
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("93.184.216.34", 443),
                    )
                ],
                [
                    (
                        socket.AF_INET6,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("2606:4700:4700::1111", 443, 0, 0),
                    )
                ],
            ]
        )

        with mock.patch.object(loop, "getaddrinfo", new=resolve):
            loaded = await avatar_loader(session).from_url(
                "https://images.example/avatar.webp",
            )

        self.assertEqual(loaded.avatar_bytes, b"webp-avatar")
        self.assertEqual(
            [call.args[:2] for call in resolve.await_args_list],
            [("images.example", 443), ("cdn.example", 443)],
        )
        self.assertEqual(
            [call.args[0] for call in session.get.call_args_list],
            [
                "https://images.example/avatar.webp",
                "https://cdn.example/avatar.webp",
            ],
        )
        for request in session.get.call_args_list:
            self.assertFalse(request.kwargs["allow_redirects"])

    async def test_unsafe_url_destinations_are_rejected_before_http(self) -> None:
        unsafe_urls = (
            "http://images.example/avatar.png",
            "https://user:password@images.example/avatar.png",
            "https://10.0.0.1/avatar.png",
            "https://127.0.0.1/avatar.png",
            "https://169.254.1.1/avatar.png",
            "https://240.0.0.1/avatar.png",
            "https://224.0.0.1/avatar.png",
            "https://0.0.0.0/avatar.png",
            "https://[::1]/avatar.png",
            "https://[ff02::1]/avatar.png",
        )
        loop = asyncio.get_running_loop()

        for url in unsafe_urls:
            with self.subTest(url=url):
                session = SimpleNamespace(
                    get=mock.Mock(
                        return_value=FakeResponse(
                            headers={"Content-Type": "image/png"},
                            chunks=(b"avatar",),
                        )
                    )
                )
                resolve = mock.AsyncMock()
                with (
                    mock.patch.object(loop, "getaddrinfo", new=resolve),
                    self.assertRaises(ValueError),
                ):
                    await avatar_loader(session).from_url(url)
                session.get.assert_not_called()
                resolve.assert_not_awaited()

    async def test_mixed_public_and_private_dns_answers_are_rejected(self) -> None:
        session = SimpleNamespace(get=mock.Mock())
        loop = asyncio.get_running_loop()
        resolve = mock.AsyncMock(
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", 443),
                ),
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", 443),
                ),
            ]
        )

        with (
            mock.patch.object(loop, "getaddrinfo", new=resolve),
            self.assertRaisesRegex(ValueError, "public network"),
        ):
            await avatar_loader(session).from_url(
                "https://images.example/avatar.png",
            )

        session.get.assert_not_called()

    async def test_private_redirect_target_is_rejected_before_request(self) -> None:
        session = SimpleNamespace(
            get=mock.Mock(
                return_value=FakeResponse(
                    status=302,
                    headers={"Location": "https://127.0.0.1/avatar.png"},
                )
            )
        )
        loop = asyncio.get_running_loop()
        resolve = mock.AsyncMock(
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", 443),
                )
            ]
        )

        with (
            mock.patch.object(loop, "getaddrinfo", new=resolve),
            self.assertRaisesRegex(ValueError, "public network"),
        ):
            await avatar_loader(session).from_url(
                "https://images.example/avatar.png",
            )

        session.get.assert_called_once()
        resolve.assert_awaited_once()

    async def test_url_bytes_are_bounded_while_streaming(self) -> None:
        response = FakeResponse(
            headers={"Content-Type": "image/gif"},
            chunks=(b"x" * (2 * 1024 * 1024), b"x"),
        )
        session = SimpleNamespace(get=mock.Mock(return_value=response))
        loop = asyncio.get_running_loop()
        resolve = mock.AsyncMock(
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", 443),
                )
            ]
        )

        with (
            mock.patch.object(loop, "getaddrinfo", new=resolve),
            self.assertRaisesRegex(ValueError, "2 MiB"),
        ):
            await avatar_loader(session).from_url(
                "https://images.example/avatar.gif",
            )

    async def test_attachment_media_type_allowlist_is_exact(self) -> None:
        for media_type in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            with self.subTest(media_type=media_type):
                loaded = await bot_proxy_avatar.load_avatar(
                    SimpleNamespace(),
                    SimpleNamespace(
                        size=6,
                        content_type=media_type,
                        read=mock.AsyncMock(return_value=b"avatar"),
                    ),
                )
                self.assertEqual(loaded.avatar_media_type, media_type)

        for media_type in (None, "text/plain", "image/svg+xml", "image/jpg"):
            with self.subTest(media_type=media_type):
                attachment = SimpleNamespace(
                    size=6,
                    content_type=media_type,
                    read=mock.AsyncMock(return_value=b"avatar"),
                )
                with self.assertRaises(ValueError):
                    await bot_proxy_avatar.load_avatar(
                        SimpleNamespace(),
                        attachment,
                    )
                attachment.read.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
