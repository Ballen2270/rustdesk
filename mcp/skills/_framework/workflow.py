"""Declarative screen signatures and a workflow-node engine for skill runners.

A skill declares:

- ``Screen``\ s — how to recognize a game screen from OCR items (text
  signatures on normalized coordinates, with disambiguation via ``forbid``);
- ``Node``\ s — one workflow step per (screen, situation): what to do
  (``act``), which screens confirm the step succeeded (``expect``), and how
  long to wait before giving up (``timeout``).

The engine is input/output agnostic: classification works on plain OCR item
dicts and node selection is a pure function of the observed state. The
runner owns capture, input (always humanized) and the safety invariants
(recheck before input, pending-confirmation, pause-don't-guess).

Data only, no I/O — trivially unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from _framework.vision import text_hits

DEFAULT_SCORE = .25


@dataclass
class Screen:
    """One game screen. ``any_of`` alternatives: each is a list of
    ``(text, box)`` or ``(text, box, min_score)`` hits that must ALL match.
    ``forbid`` hits disqualify the screen regardless of alternatives."""
    name: str
    any_of: list
    forbid: list = field(default_factory=list)
    label: str = ''       # viewer badge text (defaults to name)
    color: str = ''

    def match(self, items):
        for e in self.forbid:
            t, b, s = e if len(e) == 3 else (*e, DEFAULT_SCORE)
            if text_hits(items, t, b, s):
                return False
        for alt in self.any_of:
            hits = [e if len(e) == 3 else (*e, DEFAULT_SCORE) for e in alt]
            if all(text_hits(items, t, b, s) for t, b, s in hits):
                return True
        return False


@dataclass
class ClickPoint:
    """Click a fixed normalized anchor box (stable UI, e.g. the 活动 button)."""
    box: tuple
    button: str = 'left'


@dataclass
class ClickText:
    """Click the center of the OCR item containing `text` inside `box`."""
    text: str
    box: tuple
    button: str = 'left'


@dataclass
class ClickEntry:
    """In a list panel, find the row whose text contains `entry` and click
    the sibling `button` text in the same row band. If the entry is not
    visible, scroll one humanized intent per attempt (runner-side) until
    max_scrolls is exceeded (→ pause)."""
    entry: str
    button: str
    search_box: tuple
    max_scrolls: int = 4
    scroll_box: tuple = (.45, .25, .35, .40)
    scroll_dir: int = 1


@dataclass
class Nop:
    """Observe only; keep the runner's state machine moving."""


@dataclass
class Node:
    """Workflow step: when the runner observes screen `source` (and `guard`
    passes), perform `act`, then wait until one of `expect` shows up.
    Unconfirmed after `timeout` seconds → runner pauses the window (never
    blind-retries an input)."""
    id: str
    source: str
    act: object
    expect: list
    timeout: float = 20.0
    guard: object = None      # callable(ctx) -> bool


class Workflow:
    def __init__(self, screens, nodes):
        self.screens = list(screens)
        self.nodes = {n.id: n for n in nodes}
        self.by_source = {}
        for n in nodes:
            self.by_source.setdefault(n.source, []).append(n)

    def classify(self, items):
        """First matching screen in declaration order; 'unknown' otherwise.
        Order matters: put more specific screens (menus, modals) first."""
        for screen in self.screens:
            if screen.match(items):
                return screen.name
        return 'unknown'

    def nodes_for(self, state, ctx=None):
        return [n for n in self.by_source.get(state, ())
                if n.guard is None or n.guard(ctx)]

    def screen(self, name):
        return next(s for s in self.screens if s.name == name)


def find_text(items, text, box=None):
    """Best OCR item whose text contains `text` and whose center lies in
    `box` (whole screen when None). Highest confidence wins. Returns the
    item (with x/y/w/h normalized) or None."""
    best = None
    for it in items:
        if text not in it['text'] or it['score'] < DEFAULT_SCORE:
            continue
        if box is not None:
            x, y, w, h = box
            cx, cy = it['x']+it['w']/2, it['y']+it['h']/2
            if not (x <= cx <= x+w and y <= cy <= y+h):
                continue
        if best is None or it['score'] > best['score']:
            best = it
    return best


def find_row_button(items, entry, button, search_box, band=.08):
    """Find the `entry` text, then the nearest `button` item in the same row
    band, to the right of the entry. Game list buttons sit slightly below
    their row text, so bbox overlap is unreliable — match by center distance
    instead (rows are ~.13 apart, so `band`=.08 cannot reach a neighbor row).
    Both the entry and the button must lie inside `search_box`. Returns
    (target, button_or_None). Used by ClickEntry."""
    target = find_text(items, entry, search_box)
    if target is None:
        return None, None
    tcy = target['y']+target['h']/2
    tcx = target['x']+target['w']/2
    sx, sy, sw, sh = search_box
    best = None
    for it in items:
        if button not in it['text'] or it['score'] < DEFAULT_SCORE:
            continue
        bcx, bcy = it['x']+it['w']/2, it['y']+it['h']/2
        if bcx <= tcx or not (sx <= bcx <= sx+sw and sy <= bcy <= sy+sh):
            continue  # right of the entry, inside the list area
        dy = abs(bcy-tcy)
        if dy <= band and (best is None or dy < best[0]):
            best = (dy, it)
    return target, (best[1] if best else None)


def level_of(items):
    """Extract the current level number from '第N关' items. Returns int or
    None. 秘境-style progression screens show '第N关：挑战XXX'."""
    for it in items:
        m = re.search(r'第(\d{1,2})关', it['text'])
        if m and it['score'] >= DEFAULT_SCORE:
            return int(m.group(1))
    return None
