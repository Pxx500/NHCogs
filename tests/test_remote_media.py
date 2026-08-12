"""Behavioral tests for bounded remote-media classification."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from socket import AF_INET
from tempfile import TemporaryDirectory
from unittest import mock

from tests.harness import _isolated_honeypot_modules


def _chunk(kind: bytes, payload: bytes) -> bytes:
    padding = b"\x00" if len(payload) % 2 else b""
    return kind + len(payload).to_bytes(4, "little") + payload + padding


def _webp(*chunks: bytes) -> bytes:
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _uint24(value: int) -> bytes:
    return value.to_bytes(3, "little")


def _animation_frame(
    *,
    x: int = 0,
    y: int = 0,
    width: int = 1,
    height: int = 1,
) -> bytes:
    vp8_key_frame = (
        b"\x10\x00\x00"
        b"\x9d\x01\x2a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
    )
    frame_header = (
        _uint24(x // 2)
        + _uint24(y // 2)
        + _uint24(width - 1)
        + _uint24(height - 1)
        + _uint24(0)
        + b"\x00"
    )
    return frame_header + _chunk(b"VP8 ", vp8_key_frame)


class WebPAnimationClassificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.modules = _isolated_honeypot_modules(Path(self.directory.name))
        self.modules.__enter__()
        self.classify = import_module(
            "Honeypot.remote_media"
        ).classify_webp_animation

    def tearDown(self):
        self.modules.__exit__(None, None, None)
        self.directory.cleanup()

    def test_complete_extended_animation_proves_animation(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)

        self.assertIs(
            self.classify(
                _webp(
                    _chunk(b"VP8X", vp8x),
                    _chunk(b"ANIM", b"\x00" * 6),
                    _chunk(b"ANMF", _animation_frame()),
                    _chunk(b"ANMF", _animation_frame()),
                )
            ),
            True,
        )

    def test_complete_static_webp_is_not_animated(self):
        vp8x = b"\x00" * 10

        self.assertIs(self.classify(_webp(_chunk(b"VP8X", vp8x))), False)
        self.assertIs(
            self.classify(_webp(_chunk(b"VP8 ", b"still-image"))),
            False,
        )

    def test_animation_chunk_proves_animation_with_odd_chunk_padding(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        odd_unknown = _chunk(b"EXIF", b"x")

        self.assertIs(
            self.classify(
                _webp(
                    _chunk(b"VP8X", vp8x),
                    odd_unknown,
                    _chunk(b"ANIM", b"\x00" * 6),
                    _chunk(b"ANMF", _animation_frame()),
                    _chunk(b"ANMF", _animation_frame()),
                )
            ),
            True,
        )

    def test_complete_animation_frame_is_required(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        full = _webp(
            _chunk(b"VP8X", vp8x),
            _chunk(b"ANIM", b"\x00" * 6),
            _chunk(b"ANMF", _animation_frame()),
            _chunk(b"ANMF", _animation_frame()),
            _chunk(b"ANMF", _animation_frame()),
        )

        self.assertIs(self.classify(full), True)
        self.assertIsNone(self.classify(full[:-1]))

    def test_single_frame_or_incomplete_riff_is_not_proven_animated(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        single_frame = _webp(
            _chunk(b"VP8X", vp8x),
            _chunk(b"ANIM", b"\x00" * 6),
            _chunk(b"ANMF", _animation_frame()),
        )
        declared_larger = bytearray(single_frame)
        declared_larger[4:8] = (0xFFFFFFFF).to_bytes(4, "little")

        self.assertIsNone(self.classify(single_frame))
        self.assertIsNone(self.classify(bytes(declared_larger)))

    def test_animation_frame_must_fit_the_declared_canvas(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        outside_canvas = _webp(
            _chunk(b"VP8X", vp8x),
            _chunk(b"ANIM", b"\x00" * 6),
            _chunk(b"ANMF", _animation_frame(width=2)),
            _chunk(b"ANMF", _animation_frame()),
        )

        self.assertIsNone(self.classify(outside_canvas))

    def test_partial_or_misordered_animation_structure_is_inconclusive(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        cases = (
            _webp(_chunk(b"VP8X", vp8x)),
            _webp(_chunk(b"ANIM", b"\x00" * 6)),
            _webp(_chunk(b"ANMF", b"\x00" * 16)),
            _webp(_chunk(b"VP8X", vp8x), _chunk(b"VP8 ", b"still")),
            _webp(_chunk(b"VP8X", vp8x), _chunk(b"VP8X", vp8x)),
            _webp(
                _chunk(b"VP8X", vp8x),
                _chunk(b"ANIM", b"\x00" * 6),
                _chunk(b"ANMF", b"\x00" * 16),
            ),
            _webp(
                _chunk(b"VP8X", vp8x),
                _chunk(b"ANIM", b"\x00" * 6),
                _chunk(b"ANMF", (b"\x00" * 16) + _chunk(b"VP8 ", b"frame")),
            ),
            _webp(
                _chunk(b"VP8X", bytes((0b00000011,)) + (b"\x00" * 9)),
                _chunk(b"ANIM", b"\x00" * 6),
                _chunk(b"ANMF", _animation_frame()),
            ),
            _webp(
                _chunk(b"VP8X", vp8x),
                _chunk(b"ANIM", b"\x00" * 6),
                _chunk(
                    b"ANMF",
                    (b"\x00" * 15) + b"\x04" + _chunk(b"VP8 ", b"frame"),
                ),
            ),
        )

        for value in cases:
            with self.subTest(value=value):
                self.assertIsNone(self.classify(value))

    def test_invalid_or_truncated_webp_is_inconclusive(self):
        valid = _webp(_chunk(b"VP8X", b"\x00" * 10))

        for value in (
            b"not webp",
            valid[:11],
            valid[:-1],
            b"RIFF\xff\xff\xff\xffWEBP",
        ):
            with self.subTest(value=value):
                self.assertIsNone(self.classify(value))
        self.assertIsNone(self.classify(_webp(_chunk(b"ANIM", b""))))
        self.assertIsNone(self.classify(_webp(_chunk(b"ANMF", b"\x00" * 15))))


class RemoteWebPInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.modules = _isolated_honeypot_modules(Path(self.directory.name))
        self.modules.__enter__()
        self.remote_media = import_module("Honeypot.remote_media")

    async def asyncTearDown(self):
        self.modules.__exit__(None, None, None)
        self.directory.cleanup()

    async def test_unsafe_urls_are_rejected_before_dns_or_http(self):
        resolver = mock.AsyncMock()
        session_factory = mock.Mock()
        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=resolver,
            session_factory=session_factory,
        )

        for url in (
            "http://media.example/reaction.webp",
            "https://user:pass@media.example/reaction.webp",
            "https://media.example:8443/reaction.webp",
            "https://127.0.0.1/reaction.webp",
            "https://[::1]/reaction.webp",
        ):
            with self.subTest(url=url):
                self.assertIsNone(await inspector.inspect(url))

        resolver.assert_not_awaited()
        session_factory.assert_not_called()

    async def test_non_global_dns_answer_is_rejected_before_http(self):
        resolver = mock.AsyncMock(
            return_value=[(AF_INET, "203.0.113.7"), (AF_INET, "127.0.0.1")]
        )
        session_factory = mock.Mock()
        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=resolver,
            session_factory=session_factory,
        )

        self.assertIsNone(
            await inspector.inspect("https://media.example/reaction.webp")
        )

        session_factory.assert_not_called()

    async def test_request_is_pinned_bounded_and_classifies_animated_webp(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        payload = _webp(
            _chunk(b"VP8X", vp8x),
            _chunk(b"ANIM", b"\x00" * 6),
            _chunk(b"ANMF", _animation_frame()),
            _chunk(b"ANMF", _animation_frame()),
        )
        response = _FakeResponse(206, [payload])
        session_factory = _SessionFactory(response)
        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")]),
            session_factory=session_factory,
        )

        result = await inspector.inspect("https://media.example/reaction.webp")

        self.assertIs(result, True)
        request = session_factory.request
        self.assertEqual(request["url"], "https://media.example/reaction.webp")
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(request["headers"]["Range"], "bytes=0-262143")
        self.assertFalse(session_factory.kwargs["trust_env"])
        self.assertFalse(session_factory.kwargs["auto_decompress"])
        pinned = await session_factory.kwargs["connector"]._resolver.resolve(
            "media.example", 443, AF_INET
        )
        self.assertEqual([item["host"] for item in pinned], ["93.184.216.34"])

    async def test_redirect_and_oversized_body_are_inconclusive(self):
        resolver = mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")])
        for response in (
            _FakeResponse(302, []),
            _FakeResponse(200, [b"x" * (256 * 1024), b"overflow"]),
        ):
            with self.subTest(status=response.status):
                inspector = self.remote_media.RemoteMediaInspector(
                    resolve_host=resolver,
                    session_factory=_SessionFactory(response),
                )
                self.assertIsNone(
                    await inspector.inspect("https://media.example/reaction.webp")
                )

    async def test_repeated_url_reuses_cached_classification(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        session_factory = _SessionFactory(
            _FakeResponse(
                206,
                [
                    _webp(
                        _chunk(b"VP8X", vp8x),
                        _chunk(b"ANIM", b"\x00" * 6),
                        _chunk(b"ANMF", _animation_frame()),
                        _chunk(b"ANMF", _animation_frame()),
                    )
                ],
            )
        )
        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")]),
            session_factory=session_factory,
        )
        url = "https://media.example/reaction.webp"

        self.assertIs(await inspector.inspect(url), True)
        self.assertIs(await inspector.inspect(url), True)

        self.assertEqual(session_factory.call_count, 1)

    async def test_dns_timeout_is_fail_open_and_cached_briefly(self):
        async def stalled_resolver(hostname):
            await asyncio.Event().wait()

        resolver = mock.AsyncMock(side_effect=stalled_resolver)
        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=resolver,
            session_factory=mock.Mock(),
        )
        url = "https://media.example/reaction.webp"

        with mock.patch.object(
            self.remote_media,
            "WEBP_DNS_TIMEOUT_SECONDS",
            0.01,
        ):
            self.assertIsNone(await inspector.inspect(url))
            self.assertIsNone(await inspector.inspect(url))

        self.assertEqual(resolver.await_count, 1)

    async def test_dns_resolution_obeys_global_inspection_concurrency(self):
        active = 0
        maximum = 0
        release = asyncio.Event()

        async def resolver(hostname):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await release.wait()
            active -= 1
            return [(AF_INET, "93.184.216.34")]

        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=resolver,
            session_factory=lambda **kwargs: _FakeSession(
                _SessionFactory(_FakeResponse(404, []))
            ),
        )
        tasks = [
            asyncio.create_task(
                inspector.inspect(f"https://media{i}.example/reaction.webp")
            )
            for i in range(4)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(maximum, 2)
        release.set()
        await asyncio.gather(*tasks)


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status, chunks):
        self.status = status
        self.content = _FakeContent(chunks)


class _FakeSession:
    def __init__(self, factory):
        self._factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    @asynccontextmanager
    async def get(self, url, **kwargs):
        self._factory.request = {"url": url, **kwargs}
        yield self._factory.response


class _SessionFactory:
    def __init__(self, response):
        self.response = response
        self.kwargs = None
        self.request = None
        self.call_count = 0

    def __call__(self, **kwargs):
        self.call_count += 1
        self.kwargs = kwargs
        return _FakeSession(self)


if __name__ == "__main__":
    unittest.main()
