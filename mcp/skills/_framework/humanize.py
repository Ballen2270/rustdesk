"""Human-like input planning shared by skill runners.

Every click and scroll a runner issues goes through this layer:

- landing points are sampled inside the target box (inner 70%), never at a
  fixed offset, and never repeating an exact coordinate for the same target;
- every action carries a randomized pre-click delay;
- arriving on a fresh screen adds a randomized "reading" pause, occasionally
  a longer hesitation;
- scroll intents are split into 2-5 wheel segments with random notches, gaps
  and a drifting anchor point.

All randomness flows from one ``random.Random`` so tests can seed it.
Coordinates are native pixels of the cropped window content.
"""
from __future__ import annotations

import random
from collections import deque


class ClickPlanner:
    def __init__(self, rng=None, inner=.70, history=50, min_repeat_dist=2.0,
                 delay_range=(.12, .45), read_range=(.30, 1.20),
                 think_prob=.08, think_range=(1.5, 2.5)):
        self.rng = rng if rng is not None else random.Random()
        self.inner = inner
        self.history_size = history
        self.min_repeat_dist = min_repeat_dist
        self.delay_range = delay_range
        self.read_range = read_range
        self.think_prob = think_prob
        self.think_range = think_range
        self._seen = {}  # target_id -> deque[(x, y)]

    def _sample(self, box, native_wh):
        x, y, w, h = box
        shrink = (1-self.inner)/2
        x, w = x+w*shrink, w*self.inner
        y, h = y+h*shrink, h*self.inner
        w, h = max(w, 1e-4), max(h, 1e-4)
        px = round((x+self.rng.random()*w)*native_wh[0])
        py = round((y+self.rng.random()*h)*native_wh[1])
        return px, py

    def point(self, target_id, box, native_wh):
        """Sample a landing pixel inside normalized `box`; enforce a minimum
        distance from this target's recent landing points. After 64 rejected
        draws (tiny box, dense history) the last draw is accepted — it is
        still random, just not novel."""
        px, py = self._sample(box, native_wh)
        for _ in range(64):
            if all(abs(px-a)+abs(py-b) >= self.min_repeat_dist
                   for a, b in self._seen.get(target_id, ())):
                break
            px, py = self._sample(box, native_wh)
        hist = self._seen.setdefault(target_id, deque(maxlen=self.history_size))
        hist.append((px, py))
        return px, py

    def delay(self, fresh_screen=False):
        """Seconds to wait before sending the input."""
        d = self.rng.uniform(*self.delay_range)
        if fresh_screen:
            d += self.rng.uniform(*self.read_range)
            if self.rng.random() < self.think_prob:
                d += self.rng.uniform(*self.think_range)
        return d


class ScrollPlanner:
    def __init__(self, rng=None, segments=(2, 5), notches=(1, 3),
                 gap_range=(.08, .25), drift=.015):
        self.rng = rng if rng is not None else random.Random()
        self.segments = segments
        self.notches = notches
        self.gap_range = gap_range
        self.drift = drift

    def segment(self, anchor, direction, native_wh):
        """Split one scroll intent at normalized `anchor` into a list of
        ``(x, y, dy, pause_after_s)`` wheel commands. direction +1 scrolls
        toward the bottom (MCP scroll dy>0 semantics). The anchor drifts a
        little per segment, like a finger not moving perfectly straight."""
        count = self.rng.randint(*self.segments)
        out = []
        ax, ay = anchor
        for _ in range(count):
            ax = min(.98, max(.02, ax+self.rng.uniform(-self.drift, self.drift)))
            ay = min(.98, max(.02, ay+self.rng.uniform(-self.drift, self.drift)))
            notches = self.rng.randint(*self.notches)
            out.append((round(ax*native_wh[0]), round(ay*native_wh[1]),
                        direction*notches, self.rng.uniform(*self.gap_range)))
        return out
