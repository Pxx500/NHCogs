"""Behavioral tests for bounded remote-media classification."""

import asyncio
import random
import threading
import unittest
from contextlib import asynccontextmanager
from importlib import import_module
from io import BytesIO
from pathlib import Path
from socket import AF_INET
from tempfile import TemporaryDirectory
from unittest import mock

from PIL import Image

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
    output = BytesIO()
    Image.new("RGBA", (width, height), "red").save(output, "WEBP", lossless=True)
    standalone = output.getvalue()
    image_chunk = standalone[12:]
    frame_header = (
        _uint24(x // 2)
        + _uint24(y // 2)
        + _uint24(width - 1)
        + _uint24(height - 1)
        + _uint24(0)
        + b"\x00"
    )
    return frame_header + image_chunk


def _animated_image(format_name: str) -> bytes:
    output = BytesIO()
    frames = [
        Image.new("RGBA", (2, 2), "red"),
        Image.new("RGBA", (2, 2), "blue"),
    ]
    frames[0].save(
        output,
        format_name,
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    return output.getvalue()


def _noisy_apng() -> bytes:
    generator = random.Random(0)
    frames = [
        Image.frombytes("RGB", (256, 256), generator.randbytes(256 * 256 * 3))
        for _index in range(3)
    ]
    output = BytesIO()
    frames[0].save(
        output,
        "PNG",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    return output.getvalue()


class WebPAnimationClassificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.modules = _isolated_honeypot_modules(Path(self.directory.name))
        self.modules.__enter__()
        self.classify = import_module(
            "NHCogs.honeypot.remote_media"
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
        second_frame_end = full.find(b"ANMF", full.find(b"ANMF") + 1)
        self.assertIsNone(self.classify(full[: second_frame_end + 16]))

    def test_bounded_prefix_with_two_complete_frames_proves_animation(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        full = bytearray(
            _webp(
                _chunk(b"VP8X", vp8x),
                _chunk(b"ANIM", b"\x00" * 6),
                _chunk(b"ANMF", _animation_frame()),
                _chunk(b"ANMF", _animation_frame()),
                _chunk(b"ANMF", _animation_frame()),
            )
        )
        full[4:8] = (512 * 1024).to_bytes(4, "little")
        third_frame = full.find(b"ANMF", full.find(b"ANMF", full.find(b"ANMF") + 1) + 1)

        self.assertIs(self.classify(bytes(full[:third_frame])), True)

    def test_corrupt_frame_payloads_do_not_prove_animation(self):
        output = BytesIO()
        frames = [
            Image.new("RGBA", (2, 2), "red"),
            Image.new("RGBA", (2, 2), "blue"),
        ]
        frames[0].save(
            output,
            "WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
            lossless=True,
        )
        corrupt = bytearray(output.getvalue())
        offset = 12
        while offset < len(corrupt):
            size = int.from_bytes(corrupt[offset + 4 : offset + 8], "little")
            payload_end = offset + 8 + size
            if corrupt[offset : offset + 4] == b"ANMF":
                image_chunk = offset + 8 + 16
                image_size = int.from_bytes(
                    corrupt[image_chunk + 4 : image_chunk + 8], "little"
                )
                image_payload = image_chunk + 8
                header_size = (
                    5
                    if corrupt[image_chunk : image_chunk + 4] == b"VP8L"
                    else 10
                )
                corrupt[
                    image_payload + header_size : image_payload + image_size
                ] = b"\xff" * (image_size - header_size)
            offset = payload_end + (size % 2)

        self.assertIsNone(self.classify(bytes(corrupt)))

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

    def test_animation_canvas_respects_the_project_pixel_budget(self):
        vp8x = (
            bytes((0b00000010,))
            + (b"\x00" * 3)
            + _uint24(3000 - 1)
            + _uint24(3000 - 1)
        )

        self.assertIsNone(
            self.classify(
                _webp(
                    _chunk(b"VP8X", vp8x),
                    _chunk(b"ANIM", b"\x00" * 6),
                    _chunk(b"ANMF", _animation_frame()),
                    _chunk(b"ANMF", _animation_frame()),
                )
            )
        )

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


class DecodedAnimationClassificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.modules = _isolated_honeypot_modules(Path(self.directory.name))
        self.modules.__enter__()
        self.remote_media = import_module("NHCogs.honeypot.remote_media")

    def tearDown(self):
        self.modules.__exit__(None, None, None)
        self.directory.cleanup()

    def test_supported_media_decoders_are_reported_available(self):
        self.assertEqual(
            self.remote_media.media_decoder_support(),
            {
                "GIF": True,
                "PNG/APNG": True,
                "WebP": True,
                "AVIF": True,
            },
        )
        if "avif" in self.remote_media.features.get_supported():
            self.assertIsNone(self.remote_media.pillow_avif)

    def test_registered_extensions_without_native_codecs_are_unavailable(self):
        with mock.patch.object(
            self.remote_media.features,
            "get_supported",
            return_value=["pil", "zlib"],
        ), mock.patch.object(self.remote_media, "pillow_avif", None):
            support = self.remote_media.media_decoder_support()

        self.assertTrue(support["GIF"])
        self.assertTrue(support["PNG/APNG"])
        self.assertFalse(support["WebP"])
        self.assertFalse(support["AVIF"])

    def test_apng_default_image_is_not_counted_as_an_animation_frame(self):
        output = BytesIO()
        Image.new("RGBA", (2, 2), "red").save(
            output,
            "PNG",
            save_all=True,
            append_images=[Image.new("RGBA", (2, 2), "blue")],
            default_image=True,
            duration=100,
            loop=0,
        )

        self.assertIs(
            self.remote_media.classify_media_animation(output.getvalue()),
            False,
        )

    def test_oversized_animation_canvas_is_rejected_before_loading_frames(self):
        image = mock.MagicMock()
        image.__enter__.return_value = image
        image.format = "PNG"
        image.size = (5000, 5000)
        image.n_frames = 2
        image.default_image = False

        with mock.patch.object(self.remote_media.Image, "open", return_value=image):
            self.assertIsNone(
                self.remote_media.classify_media_animation(b"\x89PNG\r\n\x1a\n")
            )

        image.load.assert_not_called()


class RemoteMediaInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.modules = _isolated_honeypot_modules(Path(self.directory.name))
        self.modules.__enter__()
        self.remote_media = import_module("NHCogs.honeypot.remote_media")

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
        self.assertEqual(request["headers"]["Range"], "bytes=0-8388607")
        self.assertFalse(session_factory.kwargs["trust_env"])
        self.assertFalse(session_factory.kwargs["auto_decompress"])
        pinned = await session_factory.kwargs["connector"]._resolver.resolve(
            "media.example", 443, AF_INET
        )
        self.assertEqual([item["host"] for item in pinned], ["93.184.216.34"])

    async def test_request_classifies_animated_avif(self):
        session_factory = _SessionFactory(
            _FakeResponse(200, [_animated_image("AVIF")])
        )
        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")]),
            session_factory=session_factory,
        )

        self.assertIs(
            await inspector.inspect("https://media.example/reaction.avif"),
            True,
        )

    async def test_request_classifies_apng_but_not_static_avif_or_png(self):
        resolver = mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")])
        cases = (
            ("reaction.png", _animated_image("PNG"), True),
            ("still.png", Image.new("RGBA", (2, 2), "red"), False),
            ("still.avif", Image.new("RGBA", (2, 2), "red"), False),
        )

        for filename, source, expected in cases:
            with self.subTest(filename=filename):
                if isinstance(source, Image.Image):
                    output = BytesIO()
                    source.save(output, filename.rsplit(".", 1)[1].upper())
                    payload = output.getvalue()
                else:
                    payload = source
                inspector = self.remote_media.RemoteMediaInspector(
                    resolve_host=resolver,
                    session_factory=_SessionFactory(_FakeResponse(200, [payload])),
                )
                self.assertIs(
                    await inspector.inspect(f"https://media.example/{filename}"),
                    expected,
                )

    async def test_apng_larger_than_the_old_prefix_budget_is_detected(self):
        payload = _noisy_apng()
        self.assertGreater(payload.__len__(), 256 * 1024)
        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")]),
            session_factory=_SessionFactory(_FakeResponse(200, [payload])),
        )

        self.assertIs(
            await inspector.inspect("https://media.example/reaction.apng"),
            True,
        )

    async def test_decode_timeout_keeps_its_concurrency_slot_until_thread_finishes(self):
        first_started = threading.Event()
        first_release = threading.Event()
        calls = 0

        def classify(data):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                first_release.wait()
            return False

        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")]),
            session_factory=_SessionFactory(_FakeResponse(200, [b"media"])),
        )
        inspector._decode_semaphore = asyncio.Semaphore(1)

        with (
            mock.patch.object(
                self.remote_media,
                "classify_media_animation",
                side_effect=classify,
            ),
            mock.patch.object(self.remote_media, "MEDIA_DECODE_TIMEOUT_SECONDS", 0.01),
        ):
            self.assertIsNone(
                await inspector.inspect("https://media.example/first.png")
            )
            self.assertTrue(first_started.is_set())
            second = asyncio.create_task(
                inspector.inspect("https://media.example/second.png")
            )
            await asyncio.sleep(0.03)
            self.assertEqual(calls, 1)
            first_release.set()
            self.assertIs(await second, False)

    async def test_partial_range_proves_animation_from_two_complete_frames(self):
        vp8x = bytes((0b00000010,)) + (b"\x00" * 9)
        payload = _webp(
            _chunk(b"VP8X", vp8x),
            _chunk(b"ANIM", b"\x00" * 6),
            _chunk(b"ANMF", _animation_frame()),
            _chunk(b"ANMF", _animation_frame()),
            _chunk(b"EXIF", b"x" * (self.remote_media.MAX_MEDIA_BYTES * 2)),
        )
        response = _FakeResponse(
            206,
            [payload[: self.remote_media.MAX_MEDIA_BYTES]],
        )
        inspector = self.remote_media.RemoteMediaInspector(
            resolve_host=mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")]),
            session_factory=_SessionFactory(response),
        )

        self.assertIs(
            await inspector.inspect("https://media.example/reaction.webp"),
            True,
        )

    async def test_redirect_and_oversized_body_are_inconclusive(self):
        resolver = mock.AsyncMock(return_value=[(AF_INET, "93.184.216.34")])
        for response in (
            _FakeResponse(302, []),
            _FakeResponse(
                200,
                [b"x" * self.remote_media.MAX_MEDIA_BYTES, b"overflow"],
            ),
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
            "MEDIA_DNS_TIMEOUT_SECONDS",
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
