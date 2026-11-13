"""Shared local perception primitives: Apple Vision OCR + Pillow features.

Extracted verbatim from rustdesk-jianhui so every skill runner sees the same
recognition behavior. All functions work on normalized coordinates
(0..1 relative to the game content image) — window size may vary, the 4:3-ish
ratio is what matters.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import tempfile
import threading
import time

from PIL import Image, ImageChops, ImageStat

logger = logging.getLogger(__name__)

# Two runners on one Mac (one per game window) each drive Apple Vision at ~1Hz.
# Concurrent Vision requests exhaust the ANE/Espresso stream slots after about
# a minute: CRImageReaderError.e5rtError("...execution_stream... call failed").
# One machine-wide lock serializes the perform call; the failure is transient,
# so a short retry rides it out instead of pausing the run.
_vision_thread_lock = threading.Lock()
_vision_fd = None
_vision_lock_path = os.path.join(tempfile.gettempdir(), 'rustdesk-mhxy-vision.lock')


class VisionUnavailable(RuntimeError):
    """Apple Vision is failing at the OS level (e5rt/ANE engine outage).

    Observed outages last tens of seconds to several minutes and affect every
    request, CPU-only ones included (the request still compiles an ANE
    program). Retrying inside one call cannot ride that out, so callers
    should degrade (keep pixel-only results) instead of aborting."""


# e5rt outages are minutes long; retrying inside the call wastes the budget.
_COOLDOWN_MIN_S = 30.0
_COOLDOWN_MAX_S = 120.0
_BREAKER_LOCK = threading.Lock()
_breaker = {'cooldown': None, 'until': 0.0, 'failures': 0}


def _is_engine_outage(error):
    text = str(error)
    return 'e5rt' in text or 'CRImageReaderError' in text


def _raise_if_cooling():
    with _BREAKER_LOCK:
        remaining = _breaker['until']-time.monotonic()
    if remaining > 0:
        raise VisionUnavailable(
            'Apple Vision engine outage; cooling down (%.0fs left)' % remaining)


def _note_outage(error):
    with _BREAKER_LOCK:
        first = _breaker['cooldown'] is None
        _breaker['cooldown'] = (_COOLDOWN_MIN_S if first
                                else min(_breaker['cooldown']*2, _COOLDOWN_MAX_S))
        _breaker['until'] = time.monotonic()+_breaker['cooldown']
        _breaker['failures'] += 1
        cooldown, failures = _breaker['cooldown'], _breaker['failures']
    logger.warning('vision engine outage #%d (%s: %.80s); cooling down %.0fs',
                   failures, type(error).__name__, error, cooldown)


def _note_recovery():
    with _BREAKER_LOCK:
        failures = _breaker['failures']
        _breaker.update(cooldown=None, until=0.0, failures=0)
    if failures:
        logger.info('vision recovered after %d failed call(s)', failures)


@contextlib.contextmanager
def _vision_guard():
    global _vision_fd
    import fcntl
    with _vision_thread_lock:
        if _vision_fd is None:
            _vision_fd = os.open(_vision_lock_path,
                                 os.O_CREAT | os.O_RDWR | getattr(os, 'O_CLOEXEC', 0),
                                 0o600)
        waited = time.monotonic()
        fcntl.flock(_vision_fd, fcntl.LOCK_EX)
        waited = time.monotonic()-waited
        if waited > 2:
            logger.info('vision lock wait %.1fs (another runner holds the engine)',
                        waited)
        try:
            yield
        finally:
            fcntl.flock(_vision_fd, fcntl.LOCK_UN)


def _vision_perform(build):
    _raise_if_cooling()
    last = None
    for delay in (0, .5, 1, 2, 4, 8):
        if delay:
            time.sleep(delay)  # outside the flock: other runners keep the engine
        try:
            with _vision_guard():
                result = build()
        except Exception as e:  # pyobjc surfaces ObjC errors as exceptions
            last = e
            if _is_engine_outage(e):
                # Engine-wide outage: open the cooldown breaker and stop
                # retrying inside this call (the ladder above cannot ride out
                # a minutes-long outage; it would only hold the lock).
                _note_outage(e)
                raise VisionUnavailable(
                    'Apple Vision engine outage (e5rt); retry deferred to the '
                    'cooldown') from e
            continue
        _note_recovery()
        return result
    raise last


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

    def perform():
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(['zh-Hans', 'en-US'])
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(str(err))
        return req
    req = _vision_perform(perform)
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
