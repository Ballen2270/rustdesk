"""Shared local perception primitives: Apple Vision OCR + Pillow features.

Extracted verbatim from rustdesk-jianhui so every skill runner sees the same
recognition behavior. All functions work on normalized coordinates
(0..1 relative to the game content image) — window size may vary, the 4:3-ish
ratio is what matters.
"""
from __future__ import annotations

import io
import time

from PIL import Image, ImageChops, ImageStat


def ocr(img):
    import Quartz
    import Vision
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG')
    raw = buf.getvalue()
    data = Quartz.CFDataCreate(None, raw, len(raw))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    if src is None:
        raise RuntimeError('Cannot decode OCR image')
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(['zh-Hans', 'en-US'])
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(str(err))
    items = []
    for obs in req.results() or []:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        b = obs.boundingBox()
        items.append(dict(text=str(candidate.string()).replace(' ', ''),
                          score=float(candidate.confidence()),
                          x=float(b.origin.x), y=float(1-b.origin.y-b.size.height),
                          w=float(b.size.width), h=float(b.size.height)))
    return items


def region_ocr(img, box, scale=1):
    w, h = img.size
    x, y, a, b = box
    left, top, right, bottom = round(x*w), round(y*h), round((x+a)*w), round((y+b)*h)
    region = img.crop((left, top, right, bottom))
    items = ocr(region.resize((region.width*scale, region.height*scale)))
    for it in items:
        it.update(x=(left+it['x']*region.width)/w, y=(top+it['y']*region.height)/h,
                  w=it['w']*region.width/w, h=it['h']*region.height/h)
    return items


def green_label(img, item):
    w, h = img.size
    x, y, a, b = (item[k] for k in ('x', 'y', 'w', 'h'))
    crop = img.crop((round(x*w), round(y*h), round((x+a)*w), round((y+b)*h)))
    if not crop.width or not crop.height:
        return False
    pixels = list(crop.convert('RGB').resize((32, 16)).getdata())
    return sum(g > 70 and g > r*1.15 and g > blue*1.15 for r, g, blue in pixels)/len(pixels) >= .18


class BattleAnchors:
    """Per-window, in-memory battle anchors. Never authorize clicks or count results."""

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


def text_hits(items, text, box, min_score=.25):
    """True when `text` appears (substring) with its center inside the
    normalized `box` and confidence >= min_score. Same semantics as the
    original jianhui classify() helper."""

    def within(it):
        x, y, w, h = box
        return x <= it['x']+it['w']/2 <= x+w and y <= it['y']+it['h']/2 <= y+h
    return any(text in it['text'] and within(it) and it['score'] >= min_score for it in items)
