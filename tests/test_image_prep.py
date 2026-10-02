"""Przygotowanie obrazu do osadzenia w mailu: skalowanie, wybór formatu i cache."""

from __future__ import annotations

import io
import os
import random
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image

from perno.mailer import image_prep


def noisy_image(width: int, height: int) -> Image.Image:
    rng = random.Random(7)
    pixels = bytes(rng.randrange(256) for _ in range(width * height * 3))
    return Image.frombytes("RGB", (width, height), pixels)


class PrepareInlineImageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.src = os.path.join(self.tmp, "zrodlo.png")
        self.cache = os.path.join(self.tmp, "cache", "obraz")

    def save_source(self, image: Image.Image) -> None:
        image.save(self.src)

    def decode(self, data: bytes) -> Image.Image:
        return Image.open(io.BytesIO(data))

    def test_wide_images_are_scaled_down_keeping_the_aspect_ratio(self):
        self.save_source(Image.new("RGB", (1200, 800), "navy"))
        data, subtype = image_prep.prepare_inline_image(self.src, self.cache, max_width=600)
        image = self.decode(data)
        self.assertEqual((subtype, image.size), ("png", (600, 400)))

    def test_narrow_images_keep_their_size(self):
        self.save_source(Image.new("RGB", (300, 200), "white"))
        data, _ = image_prep.prepare_inline_image(self.src, self.cache)
        self.assertEqual(self.decode(data).size, (300, 200))

    def test_transparency_is_flattened_onto_white(self):
        self.save_source(Image.new("RGBA", (50, 50), (255, 0, 0, 0)))  # w pełni przezroczysty
        data, _ = image_prep.prepare_inline_image(self.src, self.cache)
        self.assertEqual(self.decode(data).convert("RGB").getpixel((10, 10)), (255, 255, 255))

    def test_result_is_cached_and_reused_until_the_source_changes(self):
        self.save_source(Image.new("RGB", (200, 100), "green"))
        first, _ = image_prep.prepare_inline_image(self.src, self.cache)
        self.assertTrue(os.path.exists(self.cache + ".png"))
        with mock.patch.object(Image, "open", side_effect=AssertionError("cache powinien wystarczyć")):
            again, subtype = image_prep.prepare_inline_image(self.src, self.cache)
        self.assertEqual((again, subtype), (first, "png"))
        future = time.time() + 10
        Image.new("RGB", (200, 100), "red").save(self.src)
        os.utime(self.src, (future, future))  # źródło nowsze niż cache → przeliczenie
        fresh, _ = image_prep.prepare_inline_image(self.src, self.cache)
        self.assertNotEqual(fresh, first)

    def test_a_heavy_png_falls_back_to_jpeg_and_drops_the_stale_png_cache(self):
        self.save_source(noisy_image(400, 400))
        os.makedirs(os.path.dirname(self.cache))
        with open(self.cache + ".png", "wb") as handle:
            handle.write(b"stary cache")
        os.utime(self.cache + ".png", (1, 1))  # starszy niż źródło
        data, subtype = image_prep.prepare_inline_image(self.src, self.cache, max_bytes=50_000)
        self.assertEqual(subtype, "jpeg")
        self.assertEqual(self.decode(data).format, "JPEG")
        self.assertTrue(os.path.exists(self.cache + ".jpg"))
        self.assertFalse(os.path.exists(self.cache + ".png"))

    def test_a_missing_pillow_gives_a_readable_error(self):
        self.save_source(Image.new("RGB", (10, 10)))
        with mock.patch.dict("sys.modules", {"PIL": None}), self.assertRaisesRegex(RuntimeError, "Pillow nie jest zainstalowane"):
            image_prep.prepare_inline_image(self.src, self.cache)


if __name__ == "__main__":
    unittest.main()
