from __future__ import annotations

import importlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from tests.harness import _isolated_honeypot_modules


class CaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_challenge_has_six_choices_and_one_matching_tile(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                captcha = importlib.import_module(f"{honeypot.__package__}.captcha")
                for _ in range(30):
                    descriptor = captcha.generate_challenge()
                    matches = [
                        index
                        for index, tile in enumerate(descriptor["tiles"])
                        if tile.count(descriptor["target"]) == descriptor["count"]
                    ]
                    self.assertEqual(len(descriptor["tiles"]), 6)
                    self.assertEqual(matches, [descriptor["answer"]])
                question = captcha.render_challenge(descriptor)
                self.assertNotIn("answer", question.prompt)
                with Image.open(io.BytesIO(question.image_png)) as image:
                    self.assertEqual(image.size, (900, 620))
                    self.assertEqual(image.format, "PNG")

    async def test_preparation_deduplicates_and_refuses_work_above_capacity(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                captcha = importlib.import_module(f"{honeypot.__package__}.captcha")
                worker = captcha.CaptchaPreparation(workers=1, capacity=1)
                descriptors = [captcha.generate_challenge(), captcha.generate_challenge()]
                try:
                    self.assertTrue(worker.request((1,), descriptors))
                    self.assertTrue(worker.request((1,), descriptors))
                    self.assertFalse(worker.request((2,), descriptors))
                    await worker.wait()
                    self.assertEqual(len(worker.get((1,))), 2)
                finally:
                    await worker.close()
