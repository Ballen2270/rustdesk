"""Reuse OCR only when the region's pixels and coordinate system are identical."""
from collections import OrderedDict


class RegionOCRCache:
    def __init__(self, reader, capacity=64):
        self.reader = reader
        self.capacity = capacity
        self.entries = OrderedDict()
        self.hits = 0
        self.misses = 0

    def read(self, image, box, scale=1):
        w, h = image.size
        x, y, a, b = box
        rect = (round(x*w), round(y*h), round((x+a)*w), round((y+b)*h))
        key = (image.size, image.mode, tuple(box), scale)
        pixels = image.crop(rect).tobytes()
        previous = self.entries.get(key)
        if previous is not None and previous[0] == pixels:
            self.hits += 1
            items = previous[1]
            self.entries.move_to_end(key)
        else:
            self.misses += 1
            items = self.reader(image, box, scale)
            self.entries[key] = (pixels, [dict(item) for item in items])
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
        return [dict(item) for item in items]
