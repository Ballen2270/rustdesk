import unittest
from unittest.mock import Mock

from PIL import Image

from ocr_cache import RegionOCRCache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new('RGB', (100, 100), 'white')
        self.box = (0, 0, .5, .5)
        self.reader = Mock(return_value=[dict(text='准备', score=.9, x=.1)])
        self.cache = RegionOCRCache(self.reader, capacity=2)

    def test_identical_pixels_reuse_but_returns_are_independent(self):
        first = self.cache.read(self.image, self.box)
        first[0]['text'] = 'changed'
        second = self.cache.read(self.image.copy(), self.box)
        self.assertEqual(second[0]['text'], '准备')
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual((self.cache.hits, self.cache.misses), (1, 1))

    def test_single_pixel_change_invalidates(self):
        self.cache.read(self.image, self.box)
        self.image.putpixel((1, 1), (0, 0, 0))
        self.cache.read(self.image, self.box)
        self.assertEqual(self.reader.call_count, 2)

    def test_change_outside_region_keeps_result(self):
        self.cache.read(self.image, self.box)
        self.image.putpixel((80, 80), (0, 0, 0))
        self.cache.read(self.image, self.box)
        self.assertEqual(self.reader.call_count, 1)

    def test_scale_size_mode_and_coordinates_are_separate(self):
        for image, box, scale in (
                (self.image, self.box, 1), (self.image, self.box, 2),
                (Image.new('RGB', (200, 200), 'white'), self.box, 1),
                (self.image.convert('L'), self.box, 1),
                (self.image, (.5, .5, .5, .5), 1)):
            self.cache.read(image, box, scale)
        self.assertEqual(self.reader.call_count, 5)
        self.assertEqual(len(self.cache.entries), 2)

    def test_failures_are_not_cached(self):
        self.reader.side_effect = [RuntimeError('OCR failed'), []]
        with self.assertRaises(RuntimeError):
            self.cache.read(self.image, self.box)
        self.assertEqual(self.cache.read(self.image, self.box), [])
        self.assertEqual(self.reader.call_count, 2)

    def test_windows_have_separate_caches(self):
        other = RegionOCRCache(self.reader)
        self.cache.read(self.image, self.box)
        other.read(self.image, self.box)
        self.assertEqual(self.reader.call_count, 2)


if __name__ == '__main__':
    unittest.main()
