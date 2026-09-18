"""Per-window, in-memory battle anchors. Never authorize clicks or count results."""
from PIL import Image, ImageChops, ImageStat


def green_label(img, item):
    w, h = img.size
    x, y, a, b = (item[k] for k in ('x', 'y', 'w', 'h'))
    crop = img.crop((round(x*w), round(y*h), round((x+a)*w), round((y+b)*h)))
    if not crop.width or not crop.height:
        return False
    pixels = list(crop.convert('RGB').resize((32, 16)).getdata())
    return sum(g > 70 and g > r*1.15 and g > blue*1.15 for r, g, blue in pixels)/len(pixels) >= .18


class BattleAnchors:
    # Independent fixed UI anchors: top emblem and bottom-right control icon.
    boxes = ((.36, .005, .085, .105), (.925, .875, .07, .11))

    def __init__(self):
        self.references = []
        self.size = None

    def patches(self, img):
        w, h = img.size
        return [img.crop((round(x*w), round(y*h), round((x+a)*w), round((y+b)*h)))
                .convert('RGB').resize((32, 32), Image.Resampling.BILINEAR)
                for x, y, a, b in self.boxes]

    def distance(self, patches, reference):
        return max(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean)/3
                   for a, b in zip(patches, reference))

    def learn(self, img):
        if self.size != img.size:
            self.references.clear()
            self.size = img.size
        patches = self.patches(img)
        if any(ImageStat.Stat(p.convert('L')).stddev[0] < 15 for p in patches):
            return
        if any(self.distance(patches, ref) < 5 for ref in self.references):
            return
        self.references.append(patches)
        self.references = self.references[-8:]

    def score(self, img):
        if img.size != self.size or not self.references:
            return None
        patches = self.patches(img)
        return min(self.distance(patches, ref) for ref in self.references)

    def matches(self, img):
        score = self.score(img)
        return score is not None and score <= 12
