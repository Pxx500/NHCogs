"""Shape-count challenges and bounded, off-loop image preparation."""

from __future__ import annotations

import asyncio
import io
import random
from collections import OrderedDict
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

SHAPES = ("triangles", "circles", "squares")


@dataclass(frozen=True, slots=True)
class CaptchaQuestion:
    prompt: str
    image_webp: bytes = field(repr=False)


def generate_challenge() -> dict:
    """Generate compact descriptors with exactly one matching tile."""
    rng = random.SystemRandom()
    target = rng.choice(SHAPES)
    count = rng.randint(1, 5)
    answer = rng.randrange(6)
    tiles = []
    for index in range(6):
        target_count = (
            count
            if index == answer
            else rng.choice([number for number in range(6) if number != count])
        )
        figures = [target] * target_count
        figures.extend(
            rng.choice([shape for shape in SHAPES if shape != target])
            for _ in range(rng.randint(3, 6))
        )
        rng.shuffle(figures)
        tiles.append(figures)
    return {"target": target, "count": count, "answer": answer, "tiles": tiles}


def render_challenge(descriptor: dict) -> CaptchaQuestion:
    image = Image.new("RGB", (900, 620), "#f4f6fa")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    for index, figures in enumerate(descriptor["tiles"]):
        left = 12 + (index % 3) * 298
        top = 12 + (index // 3) * 304
        draw.rounded_rectangle(
            (left, top, left + 280, top + 288), 12, fill="white", outline="#596579", width=3
        )
        draw.text((left + 15, top + 8), str(index + 1), fill="#172235", font=font)
        for position, shape in enumerate(figures):
            x = left + 23 + (position % 4) * 64
            y = top + 65 + (position // 4) * 67
            if shape == "triangles":
                draw.polygon(
                    ((x + 22, y), (x, y + 42), (x + 44, y + 42)),
                    fill="#334c81",
                    outline="#172235",
                    width=2,
                )
            elif shape == "circles":
                draw.ellipse((x, y, x + 44, y + 44), fill="#334c81", outline="#172235", width=2)
            else:
                draw.rectangle((x, y, x + 42, y + 42), fill="#334c81", outline="#172235", width=2)
    output = io.BytesIO()
    image.save(output, format="WEBP", lossless=True)
    prompt = f"Which tile contains exactly {descriptor['count']} {descriptor['target']}?"
    return CaptchaQuestion(prompt, output.getvalue())


class CaptchaPreparation:
    """A fixed worker pool with bounded queued work and ready-image cache."""

    def __init__(self, *, workers: int = 2, capacity: int = 32, cache_size: int = 128):
        self._queue = asyncio.PriorityQueue(maxsize=capacity)
        self._worker_count = workers
        self._workers: list[asyncio.Task] = []
        self._pending: set[tuple] = set()
        self._ready: OrderedDict[tuple, tuple[CaptchaQuestion, CaptchaQuestion]] = OrderedDict()
        self._errors: set[tuple] = set()
        self._discarded: set[tuple] = set()
        self._cache_size = cache_size
        self._sequence = 0
        self._closed = False

    def request(self, key: tuple, descriptors: list[dict], *, priority: int = 1) -> bool:
        if self._closed:
            return False
        if key in self._ready or key in self._pending:
            return True
        if self._queue.full():
            return False
        if not self._workers:
            self._workers = [asyncio.create_task(self._work()) for _ in range(self._worker_count)]
        self._sequence += 1
        self._errors.discard(key)
        self._pending.add(key)
        self._queue.put_nowait((priority, self._sequence, key, descriptors))
        return True

    def get(self, key: tuple) -> tuple[CaptchaQuestion, CaptchaQuestion] | None:
        value = self._ready.get(key)
        if value is not None:
            self._ready.move_to_end(key)
        return value

    def failed(self, key: tuple) -> bool:
        return key in self._errors

    async def wait(self) -> None:
        """Wait for already queued preparation, used by controlled enrollment."""
        await self._queue.join()

    def forget(self, key: tuple) -> None:
        self._ready.pop(key, None)
        self._errors.discard(key)
        if key in self._pending:
            self._discarded.add(key)

    async def _work(self) -> None:
        while True:
            _, _, key, descriptors = await self._queue.get()
            try:
                rendered = await asyncio.to_thread(
                    lambda descriptors=descriptors: tuple(
                        render_challenge(item) for item in descriptors
                    )
                )
                if key in self._discarded:
                    continue
                self._ready[key] = rendered
                while len(self._ready) > self._cache_size:
                    self._ready.popitem(last=False)
            except Exception:
                self._errors.add(key)
            finally:
                self._pending.discard(key)
                self._discarded.discard(key)
                self._queue.task_done()

    async def close(self) -> None:
        self._closed = True
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._ready.clear()
        self._errors.clear()
        self._discarded.clear()
