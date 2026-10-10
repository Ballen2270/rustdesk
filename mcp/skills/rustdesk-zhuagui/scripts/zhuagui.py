#!/usr/bin/env python3
"""Local Apple Vision/Pillow recognition and serial RustDesk MCP ghost hunting."""
from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import io
import json
import math
from pathlib import Path
import random
import re
import signal
import socket
import statistics
import subprocess
import sys
import time

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _framework.artifacts import event_logger, save, shot_name
from _framework.humanize import ClickPlanner, ScrollPlanner
from _framework.ocr_cache import RegionOCRCache
from _framework.session import load_spec, lease_keeper, make_call, open_session
from _framework.vision import BattleAnchors, ocr, region_ocr, text_hits
from _framework.window_targets import locate_targets
from _framework.workflow import (ClickEntry, ClickPoint, ClickText, Node,
                                 Screen, Workflow, find_text)

TOP = (0, 0, 1, .13)
MAP = (.04, 0, .18, .12)
LIST = (.20, .16, .74, .52)
TABS = (.03, .17, .18, .35)
TASK = (.76, .19, .24, .11)
MENU_HEAD = (.68, .18, .30, .09)
MENU_FIRST = (.70, .27, .27, .09)
MENU_OTHER = (.70, .36, .27, .36)
MODAL = (.28, .40, .44, .22)
CONFIRM = (.53, .52, .15, .11)
# "双倍点数不足" popup (provisional geometry until the reference shot lands):
# a centered modal whose 取消 sits left of the confirm column.
POPUP = (.25, .35, .50, .30)
POPUP_CANCEL = (.24, .49, .28, .16)
ROUND = (.42, 0, .16, .12)
CANCEL = (.90, .80, .10, .20)
ACTIVITY = (.20, 0, .20, .12)
# Neutral scene spot to tap after claiming: closes the lingering NPC dialog
# before the task-panel click (a dialog-visible click does not start寻路).
DIALOG_TAP = (.26, .42, .07, .06)
# Stable top-bar labels with their measured relative centers inside a window.
TOPBAR_ANCHORS = (('指引', .234), ('活动', .306), ('排行', .360))
TOPBAR_Y = .069


def _free_port(start):
    """First port >= start that nothing is listening on."""
    for cand in range(start, start+20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', cand)) != 0:
                return cand
    return None


def unique_text(items, text, box):
    hits = []
    for it in items:
        if text == '活动' and re.sub(r'[^\u4e00-\u9fff]', '', it['text']) != '活动':
            continue
        if not text_hits([it], text, box):
            continue
        cx, cy = it['x']+it['w']/2, it['y']+it['h']/2
        if not any(abs(cx-(h['x']+h['w']/2)) < .025
                   and abs(cy-(h['y']+h['h']/2)) < .02 for h in hits):
            hits.append(it)
    return hits[0] if len(hits) == 1 else None


def row_button(items):
    entry = unique_text(items, '捉鬼任务', LIST)
    if entry is None:
        return None, None
    cx, cy = entry['x']+entry['w']/2, entry['y']+entry['h']/2
    # The list has two columns; left-column rows must never reach right buttons.
    lane = (.45, .64) if cx < .60 else (.82, .94)
    hits = [it for it in items if text_hits([it], '参加', LIST)
            and lane[0] <= it['x']+it['w']/2 <= lane[1]
            and abs(it['y']+it['h']/2-cy) < .065]
    return entry, unique_text(hits, '参加', LIST)


def battle_hits(items):
    toolbar = (.55, .5, .45, .5)
    if sum(text_hits(items, t, toolbar) for t in ('法术', '攻击', '道具', '防御', '自动')) >= 3:
        return True
    digit = any(it['text'].isdigit() and text_hits([it], it['text'], ROUND, .25)
                for it in items)
    return digit and text_hits(items, '取消', CANCEL, .5)


def build_screens():
    # Variable decorations (查询次数, ghost names, second labels beside 活动)
    # are deliberately not required: any two core marks are enough.
    return [
        Screen('round_done', [[('捉完', MODAL), ('轮鬼', MODAL)],
                              [('是否继续', MODAL), ('确定', CONFIRM)]]),
        Screen('insufficient_popup', [[('双倍', POPUP), ('不足', POPUP)],
                                      [('点数不足', POPUP)]]),
        Screen('claim_menu', [[('请选择要做的事', MENU_HEAD), ('捉鬼任务', MENU_FIRST)]]),
        Screen('activity_panel', [[('活动', (.28, .07, .4, .11)), ('日常活动', TABS)]]),
        Screen('task', [[('捉鬼', TASK)]], forbid=[('取消', CANCEL)]),
        Screen('normal', [[('活动', ACTIVITY)]],
               forbid=[('捉鬼', TASK), ('请选择要做的事', MENU_HEAD),
                       ('捉鬼任务', MENU_FIRST), ('捉完', MODAL),
                       ('是否继续', MODAL), ('取消', CANCEL),
                       ('日常活动', TABS)]),
    ]


class ZhuaguiWorkflow(Workflow):
    def classify(self, items):
        # Partial foreground modal evidence must not expose the background task.
        if self.screens[0].match(items):
            return 'round_done'
        if text_hits(items, '捉完', MODAL) or text_hits(items, '是否继续', MODAL):
            return 'unknown'
        if text_hits(items, '请选择要做的事', MENU_HEAD):
            menu = next(x for x in self.screens if x.name == 'claim_menu')
            return 'claim_menu' if menu.match(items) else 'unknown'
        if battle_hits(items):
            return 'battle'
        return super().classify(items)


def build_workflow(cfg):
    return ZhuaguiWorkflow(build_screens(), [
        Node('open_activity', 'normal', ClickText('活动', ACTIVITY), ['activity_panel'], 20),
        Node('select_daily', 'activity_panel', ClickText('日常活动', TABS), ['activity_panel'], 20),
        Node('join_entry', 'activity_panel', ClickEntry('捉鬼任务', '参加', LIST,
             cfg.get('max_scrolls', 8), tuple(cfg['activity_list_scroll'])), ['claim_menu'], 150),
        Node('claim_task', 'claim_menu', ClickText('捉鬼任务', MENU_FIRST), ['task', 'battle'], 30),
        Node('dismiss_dialog', 'task', ClickPoint(DIALOG_TAP),
             ['task', 'battle', 'normal', 'claim_menu', 'unknown'], 20),
        Node('goto_ghost', 'task', ClickText('捉鬼', TASK), ['battle', 'round_done'], 150),
        Node('continue_round', 'round_done', ClickText('确定', CONFIRM), ['claim_menu'], 150),
        Node('dismiss_insufficient', 'insufficient_popup', ClickText('取消', POPUP_CANCEL),
             ['task', 'battle', 'claim_menu', 'normal', 'unknown'], 20),
        Node('dismiss_overlay', 'unknown', ClickPoint(DIALOG_TAP),
             ['normal', 'activity_panel', 'claim_menu', 'task', 'unknown'], 20),
    ])


def task_progress(items):
    for it in items:
        if text_hits([it], '捉鬼', TASK):
            m = re.search(r'(\d{1,2})\s*/\s*(\d{1,2})', it['text'])
            if m:
                return int(m[1]), int(m[2])
    return None, None


def map_position(items):
    for it in items:
        if it['score'] < .25 or not text_hits([it], it['text'], MAP):
            continue
        # Vision often reads the small-font full-width comma as '.'/'、'/「。」.
        m = re.search(r'[（(]\s*(\d{1,3})\s*[,，.、。．]\s*(\d{1,3})\s*[)）]', it['text'])
        if m:
            return [int(m[1]), int(m[2])]
    return None


def snap_from_anchors(items, size, rect, others):
    """Estimate a window's content top-left from top-bar labels visible in a
    full-device frame. Hits must sit on the expected top-bar row (the chat
    marquee also contains 活动, one row lower) and at least one rare label
    (指引/排行) must be present; a lone 活动 match anchors nothing."""
    W, H = size
    x0, y0, w, h = rect
    y_expect = y0 + TOPBAR_Y*h

    def owner(cx):
        for rx, _, rw, _ in others:
            if abs(cx-(rx+rw/2)) < abs(cx-(x0+w/2)):
                return False
        return True
    est_x, est_y, labels = [], [], set()
    for it in items:
        if it['score'] < .5:
            continue
        cx, cy = (it['x']+it['w']/2)*W, (it['y']+it['h']/2)*H
        if abs(cy-y_expect) > 36:
            continue
        for text, rel in TOPBAR_ANCHORS:
            if text in it['text'] and owner(cx):
                est_x.append(cx-rel*w)
                est_y.append(cy-TOPBAR_Y*h)
                labels.add(text)
                break
    if len(est_x) < 2 or not ({'指引', '排行'} & labels):
        return None
    # Clamp loosely: a stale configured width must not drag the estimate back
    # (the caller trims width to the device edge instead).
    x = max(0, min(round(sum(est_x)/len(est_x)), W-100))
    y = max(0, min(round(sum(est_y)/len(est_y)), H-100))
    return [x, y, w, h]


def topbar_strip_items(img, y_hint):
    """OCR the full-width top strip around `y_hint`, upscaled 2x — the labels
    are ~20px tall and a plain full-frame pass misses them."""
    W, H = img.size
    top = max(0, round(y_hint)-6)
    height = min(H-top, 110)
    return region_ocr(img, (0, top/H, 1, (top+height)/H), 2)


def titlebar_runs(img, y_hint=None):
    """Segment a title-bar strip into window spans across the wallpaper gaps:
    uniform saturated columns are wallpaper, everything else is a window
    (title bars are white or dark, their icons/text stay inside the span).
    Without y_hint the strip is the screen top (windows pinned there); with
    it, the strip sits just above the configured content top — with four
    tiled windows the screen-top strip belongs to the upper row, not ours.
    Returns [(x_start, x_end, content_top)]. Fallback window locator when
    the game top bar is not OCR-readable (e.g. both clients sit in battle)."""
    W, H = img.size
    rgb = img.convert('RGB')
    strip = list(range(2, min(30, H)))
    banded = False
    if y_hint is not None and 10 <= y_hint < H:
        band = list(range(max(2, y_hint - 30), y_hint))
        if len(band) >= 8:
            strip, banded = band, True

    def wallpaper(x):
        vals = [rgb.getpixel((x, y)) for y in strip]
        sat = [max(v)-min(v) for v in vals]
        lum = [sum(v)/3 for v in vals]
        return max(lum)-min(lum) < 40 and sum(s > 60 for s in sat) >= .6*len(strip)
    spans, x = [], 0
    while x < W:
        if not wallpaper(x):
            start = x
            while x < W and not wallpaper(x):
                x += 1
            if x-start >= 300:
                mid = (start+x)//2
                # The title bar anchors the content-top walk: a pinned window
                # starts it at the strip top, a banded strip ends it at the
                # configured content top.
                y = strip[-1] if banded else 2
                base = rgb.getpixel((mid, y))
                cap = strip[-1] + 50 if banded else 80
                while y < min(cap, H):
                    v = rgb.getpixel((mid, y))
                    if max(v)-min(v) >= 32 or abs(sum(v)/3-sum(base)/3) > 60:
                        break
                    y += 1
                spans.append((start, x, y))
        else:
            x += 1
    return spans


def recognize(img, wf, anchors=None, active=False, cache=None):
    started = time.monotonic()
    read = cache.read if cache is not None else region_ocr
    items = []
    # Always scan the modal before classifying background UI.
    for box in (MODAL, TOP, TABS, MENU_HEAD, MENU_FIRST, MENU_OTHER, TASK, ROUND, CANCEL):
        items += read(img, box)
    source = 'roi_ocr'
    state = wf.classify(items)
    if state in ('unknown', 'normal', 'task'):
        items += read(img, MAP, 3)
        items += read(img, (.27, .04, .075, .065), 3)
        items += read(img, MODAL, 2)
        items += read(img, TASK, 2)
        items += read(img, ROUND, 3)
        state = wf.classify(items)
    if state == 'activity_panel' or text_hits(items, '活动', (.28, .07, .4, .11)):
        items += read(img, TABS, 2)
        items += read(img, LIST, 2)
        state = wf.classify(items)
    if state == 'unknown':
        items += ocr(img)
        state = wf.classify(items)
        source = 'full_ocr_fallback'
    if state == 'unknown' and active and anchors is not None and anchors.matches(img):
        # Never maintain battle over an unresolved foreground modal/menu.
        if not any(text_hits(items, t, b) for t, b in (
                ('捉完', MODAL), ('是否继续', MODAL), ('请选择要做的事', MENU_HEAD))):
            state, source = 'battle', 'local_battle_anchors'
    n, total = task_progress(items)
    return dict(state=state, source=source, task_n=n, task_total=total,
                map_position=map_position(items),
                recognition_seconds=round(time.monotonic()-started, 4)), items


def make_state(target, mode='single', points=None):
    return dict(count=0, target=target, mode=mode, points=points,
                estimated_spent=0, active=False, round_open=False,
                round_counted=False, battle_seen=False, initialized=False)


def cleanup_frames(output):
    for path in output.glob('*.png'):
        if not path.name.endswith(('-before.png', '-after.png')):
            path.unlink()


def begin_action(s, action, now):
    s.update(pending=action, sent=now)
    if action == 'open_activity':
        s['daily_selected'] = False
    elif action == 'dismiss_dialog':
        s.pop('dialog_pending', None)
    elif action == 'claim_task':
        s.update(round_open=True, round_counted=False, battle_seen=False,
                 task_n=None, nav_attempts=0, task_since=now)
        s.pop('continue_delay', None)
        s.pop('continue_at', None)
        s.pop('position', None)
        s.pop('still_since', None)
    elif action == 'goto_ghost':
        s['nav_attempts'] = s.get('nav_attempts', 0)+1
        s['nav_position'] = s.get('position')
    elif action == 'continue_round':
        s['continuing'] = True


def advance(s, obs, now, wf, cfg, execute=True, rng=None):
    """Count only owned rounds with observed combat, gate inputs on fresh evidence."""
    state = obs['state']
    if s.get('paused') or s['count'] >= s['target']:
        return None
    previous = s.get('seen')
    s['streak'] = s.get('streak', 0)+1 if previous == state else 1
    s['seen'] = state
    s['fresh'] = previous != state
    if not s['initialized']:
        s.setdefault('init_since', now)
        if state in ('normal', 'activity_panel', 'claim_menu'):
            s['initialized'] = True
        elif state in ('task', 'battle'):
            # Manual play or an earlier run left a round open: adopt it —
            # its round-end popup is still valid completion evidence.
            s['initialized'] = True
            if execute:
                s.update(round_open=True, adopted=True,
                         battle_seen=state == 'battle',
                         recovered='adopted_open_round')
        elif state == 'round_done':
            s['initialized'] = True
        elif now-s['init_since'] > 20:
            # An overlay left open at startup (leaderboard, bag…) reads as
            # unknown: right-click it away before declaring the startup lost.
            if s.get('dismiss_tries', 0) < cfg.get('max_dismiss_retries', 5):
                s['dismiss_tries'] = s.get('dismiss_tries', 0)+1
                s['init_since'] = now
                return 'dismiss_overlay'
            s['paused'] = 'unknown startup; recalibrate window rectangle'
    if s.get('paused'):
        return None
    if state == 'insufficient_popup':
        # Chance popup right after claiming: no double points to spend —
        # cancel it; whatever was pending resumes once the modal is gone.
        return 'dismiss_insufficient'
    pos = obs.get('map_position')
    moved = pos is not None and s.get('position') is not None and pos != s['position']
    if pos is not None:
        if moved or s.get('position') is None:
            s['still_since'] = now
        s['position'] = pos
    n = obs.get('task_n')
    if state == 'task' and n is not None and n != s.get('task_n'):
        s.update(task_n=n, task_total=obs.get('task_total'), task_since=now, nav_attempts=0)
        s.pop('nav_at', None)
    pending = s.get('pending')
    if pending:
        # Click-time OCR may have missed the coordinate line; a later frame that
        # reads a position while the task is unchanged still anchors "still at
        # the same spot", so the single retry stays possible.
        if pending == 'goto_ghost' and s.get('nav_position') is None and pos is not None:
            s['nav_position'] = pos
        node = wf.nodes[pending]
        confirmed = state in node.expect
        if pending == 'goto_ghost' and (moved or (n is not None and n != s.get('nav_task_n'))):
            confirmed = True
        if confirmed:
            s['confirmed'] = pending
            s.pop('pending', None)
            s.pop('sent', None)
            if pending == 'continue_round':
                s.pop('continuing', None)
            if pending == 'select_daily':
                s['daily_selected'] = True
            if pending == 'claim_task':
                # The claim leaves the NPC dialog up; it must be dismissed
                # before the task-panel click will start navigation.
                s['dialog_pending'] = True
            if pending == 'goto_ghost':
                s['nav_at'] = now
        elif now-s['sent'] > node.timeout:
            if pending in ('open_activity', 'select_daily') and s.get('nav_fails', 0) < 3:
                # An overlay swallowed the click (leaderboard, bag…): close it
                # with a right-click, then line up again.
                s['nav_fails'] = s.get('nav_fails', 0)+1
                s.pop('pending', None)
                s.pop('sent', None)
                return 'dismiss_overlay'
            s['paused'] = 'action not confirmed: '+pending
        elif (pending == 'goto_ghost' and state == 'task'
              and s.get('nav_attempts') == 1 and pos is not None
              and pos == s.get('nav_position') and s['streak'] >= 2
              and now-s['sent'] >= cfg.get('navigation_retry', 8)):
            s['nav_at'] = s['sent']
            s.pop('pending', None)
            s.pop('sent', None)
    if state == 'battle':
        s['active'] = True
        if s['round_open'] and not s['round_counted']:
            s['battle_seen'] = True
    elif state != 'unknown':
        s['active'] = False
    if previous == 'battle' and state == 'task':
        s['task_since'] = now
    if execute and state in ('task', 'battle') and not s['round_open']:
        # The user (or a popup countdown race) re-engaged a round manually:
        # adopt it — its round-end popup is still valid completion evidence.
        s.update(round_open=True, adopted=True, task_since=now,
                 battle_seen=s.get('battle_seen') or state == 'battle',
                 recovered='adopted_open_round')
        s.pop('missing_since', None)
    if state == 'round_done' and not s['round_counted']:
        if not (s['round_open'] and s['battle_seen']):
            # Unowned popup: its own countdown cancels it; treat the screen
            # as a clean round boundary and re-claim from menu/panel.
            s.update(round_open=False, battle_seen=False,
                     recovered='skipped_unowned_round_end')
            s.pop('dialog_pending', None)
            s.pop('missing_since', None)
        else:
            s['count'] += 1
            s.update(round_counted=True, round_open=False)
            if s['mode'] == 'double':
                s['estimated_spent'] += 40
            if s['count'] < s['target']:
                s['continue_delay'] = (rng if rng is not None else random).uniform(10, 40)
                s['continue_at'] = now+s['continue_delay']
    if state == 'unknown':
        s.setdefault('unknown_since', now)
        if now-s['unknown_since'] > 20:
            s['warning'] = 'unknown screen; inputs withheld'
        # A stray panel (leaderboard, bag, …) that nothing drives: right-click
        # closes most game overlays — try it instead of stalling forever.
        if now-s['unknown_since'] > cfg.get('overlay_grace', 30):
            if s.get('dismiss_tries', 0) >= cfg.get('max_dismiss_retries', 5):
                s['paused'] = 'unknown screen not closable by right-click'
                return None
            s['dismiss_tries'] = s.get('dismiss_tries', 0)+1
            s.pop('unknown_since', None)
            return 'dismiss_overlay'
    else:
        s.pop('unknown_since', None)
        s.pop('dismiss_tries', None)
        s.pop('warning', None)
    if state in ('normal', 'claim_menu') and s['round_open'] and not s['round_counted']:
        s.setdefault('missing_since', now)
        if now-s['missing_since'] > cfg.get('assist_grace', 60):
            # The round-end popup was missed (unreadable screen hid it) or
            # its countdown auto-cancelled during our continue wait: drop
            # the round without counting — the points were still spent —
            # and re-claim via the normal flow instead of freezing.
            s['missed_rounds'] = s.get('missed_rounds', 0)+1
            if s['mode'] == 'double' and s.get('battle_seen'):
                s['estimated_spent'] += 40
            s.update(round_open=False, battle_seen=False,
                     recovered='missed_round_end')
            s.pop('dialog_pending', None)
            s.pop('missing_since', None)
    else:
        s.pop('missing_since', None)
    if s.get('paused') or s['count'] >= s['target'] or s.get('pending') or not s['initialized']:
        return None
    if state == 'round_done':
        return 'continue_round' if s['round_counted'] and now >= s.get('continue_at', math.inf) else None
    if s['streak'] < 2:
        return None
    if state in ('normal', 'activity_panel', 'claim_menu'):
        if s['round_open']:
            return None
        if state == 'claim_menu' and s['mode'] == 'double' and s['points']-s['estimated_spent'] < 40:
            s['paused'] = 'insufficient double-point budget for a full round'
            return None
        if state == 'activity_panel':
            return 'join_entry' if s.get('daily_selected') else 'select_daily'
        return wf.nodes_for(state)[0].id
    if state == 'task' and s['round_open']:
        s.setdefault('task_since', now)
        if s.get('dialog_pending'):
            # Freshly claimed: tap a neutral scene spot to close the NPC
            # dialog, then let the normal navigation logic take over.
            if s['streak'] >= 2 and now-s['task_since'] >= cfg.get('navigation_delay', 3):
                return 'dismiss_dialog'
            return None
        delay = cfg.get('navigation_delay', 3)
        if now-s['task_since'] < delay or moved:
            return None
        if s.get('still_since') is not None and now-s['still_since'] < delay:
            return None
        tries = s.get('nav_attempts', 0)
        if tries == 0:
            return 'goto_ghost'
        # Exactly one retry, only with a stable, recognized map position.
        if tries == 1 and pos is not None and pos == s.get('nav_position'):
            if now-s.get('nav_at', s['task_since']) >= cfg.get('navigation_retry', 8):
                return 'goto_ghost'
        if now-s['task_since'] > cfg.get('navigation_timeout', 150):
            s['paused'] = 'navigation made no battle progress; reconcile'
    return None


def targets(cfg, override=0):
    result = []
    for win in cfg['windows']:
        mode = win.get('mode', cfg.get('mode', 'single'))
        points = win.get('double_points')
        goal = override or win.get('rounds', cfg.get('rounds'))
        if goal is None:
            goal = points//40 if mode == 'double' and type(points) is int else 5
        if type(goal) is not int or not 0 <= goal <= 50:
            raise ValueError('rounds must be an integer in 0..50')
        if mode not in ('single', 'double'):
            raise ValueError('mode must be single or double')
        if mode == 'double':
            if type(points) is not int or points < 0 or goal*40 > points:
                raise ValueError('each double window requires enough double_points at 40 per round')
            if not win.get('reward_mode_confirmed'):
                raise ValueError('confirm the in-game double mode before running')
        result.append((goal, mode, points))
    return result


def validate(cfg, override=0):
    if type(cfg.get('ocr_cache', True)) is not bool:
        raise ValueError('ocr_cache must be boolean')
    if not cfg.get('calibrated'):
        raise ValueError('Requires freshly calibrated configuration')
    base_resolution = cfg.get('native_resolution')
    if not isinstance(base_resolution, list) or len(base_resolution) != 2 or any(type(v) is not int or v < 1 for v in base_resolution):
        raise ValueError('Invalid native_resolution')
    windows = cfg.get('windows', [])
    if not 1 <= len(windows) <= 2 or len({w['name'] for w in windows}) != len(windows):
        raise ValueError('Requires one or two distinct windows')
    plans = targets(cfg, override)
    rects = []
    for win in windows:
        if not re.fullmatch(r'[a-z0-9_-]+', win['name']) or not win.get('identity'):
            raise ValueError('Missing calibrated window identity or invalid name')
        dev = win.get('device', cfg.get('device'))
        if not isinstance(dev, str) or not re.fullmatch(r'[a-z0-9_-]+', dev):
            raise ValueError('Requires a valid device name')
        resolution = win.get('native_resolution', cfg.get('native_resolution'))
        if not isinstance(resolution, list) or len(resolution) != 2 or any(type(v) is not int or v < 1 for v in resolution):
            raise ValueError('Invalid native_resolution')
        rect = win.get('rect')
        if not isinstance(rect, list) or len(rect) != 4 or any(type(v) is not int for v in rect):
            raise ValueError('Invalid window rect')
        x, y, w, h = rect
        if min(x, y) < 0 or not 300 <= w <= 2560 or h < 300 or x+w > resolution[0] or y+h > resolution[1]:
            raise ValueError('Invalid content rectangle')
        for other_dev, other in rects:
            if dev == other_dev and max(x, other[0]) < min(x+w, other[0]+other[2]) and max(y, other[1]) < min(y+h, other[1]+other[3]):
                raise ValueError('Window content rectangles overlap')
        rects.append((dev, rect))
    box = cfg.get('activity_list_scroll', [.65, .25, .22, .32])
    if len(box) != 4 or any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 or v > 1 for v in box) or min(box[2:]) <= 0 or box[0]+box[2] > 1 or box[1]+box[3] > 1:
        raise ValueError('Invalid activity_list_scroll')
    cfg['activity_list_scroll'] = box
    for key, default in (('navigation_delay', 3), ('navigation_retry', 8), ('navigation_timeout', 150)):
        val = cfg.get(key, default)
        if type(val) not in (float, int) or not math.isfinite(val) or val <= 0:
            raise ValueError('Invalid '+key)
    if type(cfg.get('max_scrolls', 8)) is not int or not 2 <= cfg.get('max_scrolls', 8) <= 20:
        raise ValueError('max_scrolls must be 2..20')
    return plans


async def run(args):
    cfg = json.loads(args.config.read_text())
    plans = validate(cfg, args.rounds)
    wf = build_workflow(cfg)
    spec = load_spec(args.mcp_spec, args.repo.resolve())
    devices = sorted({w.get('device', cfg.get('device')) for w in cfg['windows']})
    locks = []
    # Acquire both shared and legacy activity locks; older runners use separate names.
    lock_devs = set(devices)
    try:
        for dev in sorted(lock_devs):
            for tag in ('device', 'jianhui', 'menpai', 'mijing', 'zhuagui'):
                lock = open(f'/private/tmp/rustdesk-{tag}-{dev}.lock', 'a')
                locks.append(lock)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        for lock in locks:
            lock.close()
        raise
    args.output.mkdir(parents=True, exist_ok=False)
    states = [make_state(*plan) for plan in plans]
    anchors = [BattleAnchors() for _ in states]
    caches = [RegionOCRCache(region_ocr) if cfg.get('ocr_cache', True) else None
              for _ in states]
    summary = dict(mode='execute' if args.execute else 'observe', status='starting',
                   games=max(p[0] for p in plans), config=cfg, windows=states)
    event = event_logger(args.output)
    stopped = False
    viewer = None
    deadline = time.monotonic()+args.minutes*60 if args.minutes else math.inf

    def request_stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)

    def stopping():
        return stopped or (args.output/'STOP').exists() or time.monotonic() >= deadline

    def persist():
        save(args.output/'state.json', summary)

    def dev_of(win):
        return win.get('device', cfg.get('device'))

    async def capture(win):
        x, y, w, h = win['rect']
        content = await call('screenshot', dev_of(win), format='png', max_width=2560,
                             wait_for_new_frame=True, crop_x=x, crop_y=y, crop_w=w, crop_h=h)
        note = ' '.join(c.text for c in content if c.type == 'text')
        dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
        if not dims or list(map(int, dims.groups())) != win.get('native_resolution', cfg['native_resolution']):
            raise RuntimeError('Resolution changed; recalibrate')
        if 'stale=True' in note:
            raise RuntimeError('Stale screenshot; no input allowed')
        blob = next(c.data for c in content if c.type == 'image')
        image = Image.open(io.BytesIO(base64.b64decode(blob))).convert('RGB')
        if image.size != (w, h):
            raise RuntimeError('Crop resized or clipped; recalibrate')
        return image

    async def autocalibrate():
        """Re-locate every window on its device before the first input.
        Windows drift between sessions (launcher, manual moves); configured
        rectangles are only a size hint, never trusted as position."""
        frames = {}
        for dev in devices:
            content = await call('screenshot', dev, format='png', max_width=2560,
                                 wait_for_new_frame=True)
            note = ' '.join(c.text for c in content if c.type == 'text')
            dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
            if not dims:
                raise RuntimeError('autocalibrate: no native resolution in note')
            native = list(map(int, dims.groups()))
            if native != cfg['native_resolution']:
                cfg['native_resolution'] = native
                event(device=dev, autocal_resolution=native)
            blob = next(c.data for c in content if c.type == 'image')
            img = Image.open(io.BytesIO(base64.b64decode(blob))).convert('RGB')
            y_hint = statistics.median(w['rect'][1] for w in cfg['windows']
                                       if dev_of(w) == dev)
            items = await asyncio.to_thread(topbar_strip_items, img, y_hint)
            frames[dev] = (img, items)
        for win in cfg['windows']:
            dev = dev_of(win)
            img, items = frames[dev]
            others = [w['rect'] for w in cfg['windows'] if w is not win and dev_of(w) == dev]
            x0, y0, w, h = win['rect']
            snapped = snap_from_anchors(items, img.size, win['rect'], others)
            source = 'topbar_anchors'
            # Pixel segmentation is content-based and immune to marquee text;
            # on any disagreement it wins. The strip follows this window's
            # configured row so tiled upper windows never win the vote.
            spans = titlebar_runs(img, y0)
            if spans:
                span = min(spans, key=lambda s: abs((s[0]+s[1])/2-(x0+w/2)))
                if snapped is None or abs(snapped[0]-span[0]) > 40 or abs(snapped[1]-span[2]) > 12:
                    if snapped is not None:
                        event(window=win['name'], autocal_conflict=dict(
                            anchors=snapped, pixels=[span[0], span[2], span[1]]))
                    snapped = [span[0], span[2], w, h]
                    source = 'titlebar_scan'
            elif snapped is None:
                raise RuntimeError('autocalibrate: no game window visible on '+dev)
            W, H = cfg['native_resolution']
            if snapped[0]+snapped[2] > W:
                snapped[2] = W-snapped[0]   # stale wide width: trim, don't shift
            if snapped[1]+snapped[3] > H:
                snapped[3] = H-snapped[1]
            win['rect'] = snapped
            event(window=win['name'], autocal_source=source)
            event(window=win['name'], autocalibrated=snapped)
        persist()

    rng = random.Random(cfg.get('rng_seed'))
    planner, scroller = ClickPlanner(rng), ScrollPlanner(rng, segments=(2, 3), notches=(1, 2))

    async def execute_node(action, win, s, obs, cache):
        node = wf.nodes[action]
        await asyncio.sleep(min(planner.delay(s.get('fresh', False)), 1))
        image = await capture(win)
        check, items = await asyncio.to_thread(recognize, image, wf, cache=cache)
        if check['state'] != obs['state']:
            event(window=win['name'], action_aborted=action, observed=check['state'])
            return
        if action == 'goto_ghost':
            if check['task_n'] != obs['task_n'] or (check['map_position'] is not None and obs['map_position'] is not None and check['map_position'] != obs['map_position']):
                event(window=win['name'], action_aborted=action, reason='task changed or already moving')
                return
        if action == 'join_entry':
            entry, item = row_button(items)
            if item is None:
                if entry is not None or find_text(items, '捉鬼任务', LIST) is not None:
                    s['paused'] = 'entry has no unique same-row participate button'
                    return
                # First sweep up, then down; bounded, each scroll rechecks the panel.
                attempt = s.get('entry_attempts', 0)
                if attempt >= cfg.get('max_scrolls', 8):
                    s['paused'] = 'ghost activity not found after bounded up/down search'
                    return
                direction = -1 if attempt < cfg.get('max_scrolls', 8)//2 else 1
                s['entry_attempts'] = attempt+1
                before = shot_name(win['name'], 'scroll-before')
                image.save(args.output/before)
                event(window=win['name'], intent='scroll', direction=direction, before=before)
                persist()
                x0, y0, w, h = win['rect']
                box = tuple(cfg['activity_list_scroll'])
                for px, py, dy, pause in scroller.segment((box[0]+box[2]/2, box[1]+box[3]/2), direction, (w, h)):
                    verify = await capture(win)
                    verified, _ = await asyncio.to_thread(recognize, verify, wf, cache=cache)
                    if verified['state'] != 'activity_panel':
                        return
                    verify.save(args.output/shot_name(win['name'], 'scroll-before'))
                    await call('scroll', dev_of(win), x=x0+px, y=y0+py, dy=dy)
                    after = await capture(win)
                    after.save(args.output/shot_name(win['name'], 'scroll-after'))
                    await asyncio.sleep(pause)
                return
        else:
            item = (None if isinstance(node.act, ClickPoint)
                    else unique_text(items, node.act.text, node.act.box))
        fixed = isinstance(node.act, ClickPoint)
        if item is None and not fixed and action != 'dismiss_insufficient':
            s['paused'] = 'missing or ambiguous click target: '+action
            return
        if item is None and action == 'dismiss_insufficient':
            return  # flaky 取消 read: the popup stays, next cycle retries
        x0, y0, w, h = win['rect']
        box = (tuple(node.act.box) if fixed
               else tuple(item[k] for k in ('x', 'y', 'w', 'h')))
        px, py = planner.point(win['name']+':'+action, box, (w, h))
        before = shot_name(win['name'], action+'-before')
        image.save(args.output/before)
        await call('mouse_move', dev_of(win), x=x0+px, y=y0+py)
        if action == 'dismiss_overlay':
            # Right-click closes most game overlays (leaderboard, bag, …).
            begin_action(s, action, time.monotonic())
            event(window=win['name'], intent=action, before=before, count=s['count'])
            persist()
            await call('click', dev_of(win), x=x0+px, y=y0+py,
                       button='right', humanize=True)
            event(window=win['name'], sent=action)
            after = await capture(win)
            after.save(args.output/shot_name(win['name'], action+'-after'))
            return
        moved_image = await capture(win)
        if not fixed:
            moved_obs, moved_items = await asyncio.to_thread(recognize, moved_image, wf, cache=cache)
            moved_item = (row_button(moved_items)[1] if action == 'join_entry'
                          else unique_text(moved_items, node.act.text, node.act.box))
            if (moved_obs['state'] != check['state'] or moved_item is None
                    or not (moved_item['x']*w <= px <= (moved_item['x']+moved_item['w'])*w
                            and moved_item['y']*h <= py <= (moved_item['y']+moved_item['h'])*h)):
                moved_image.save(args.output/shot_name(win['name'], action+'-after'))
                event(window=win['name'], action_aborted=action, reason='target changed after cursor travel')
                return
        if action == 'goto_ghost' and (moved_obs['task_n'] != check['task_n']
                or (moved_obs['map_position'] is not None and check['map_position'] is not None
                    and moved_obs['map_position'] != check['map_position'])):
            moved_image.save(args.output/shot_name(win['name'], action+'-after'))
            event(window=win['name'], action_aborted=action, reason='already navigating after cursor travel')
            return
        begin_action(s, action, time.monotonic())
        if action == 'goto_ghost':
            s['nav_task_n'] = check['task_n']
            if check['map_position'] is not None:
                s['nav_position'] = check['map_position']
        event(window=win['name'], intent=action, before=before, count=s['count'])
        persist()  # Uncertain delivery remains visible even if this call fails.
        await call('click', dev_of(win), x=x0+px, y=y0+py, humanize=True)
        event(window=win['name'], sent=action)
        after = await capture(win)
        after.save(args.output/shot_name(win['name'], action+'-after'))

    try:
        if args.viewer:
            viewer_py = Path(__file__).resolve().parents[2]/'rustdesk-jianhui/scripts/viewer.py'
            port = _free_port(args.viewer_port)
            if port is None:
                print('viewer: no free port; continuing without console', flush=True)
            else:
                viewer = subprocess.Popen([sys.executable, str(viewer_py), '--run-dir', str(args.output),
                                           '--port', str(port)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                await asyncio.sleep(1)
                ready = socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect_ex(('127.0.0.1', port)) == 0
                print(f'viewer: http://127.0.0.1:{port}'+('' if ready else ' (starting…)'), flush=True)
        if all(s['target'] == 0 for s in states):
            summary['status'] = 'complete'
        else:
            async with open_session(spec, args.repo.resolve(), {'RUSTDESK_SCREENSHOT_DIR': ''}) as session:
                call = make_call(session, stopping)
                await locate_targets(cfg, call, args.output, 'zhuagui')
                keep = lease_keeper(call, devices)
                if cfg.get('autocal', True):
                    await autocalibrate()  # windows are re-located before any input
                summary['status'] = 'running'
                while not stopping():
                    started = time.monotonic()
                    await keep(started)
                    for i, win in enumerate(cfg['windows']):
                        s = states[i]
                        if s.get('paused') or s['count'] >= s['target']:
                            continue
                        try:
                            image = await capture(win)
                            image.save(args.output/f"latest-{win['name']}.png")
                            obs, items = await asyncio.to_thread(recognize, image, wf, anchors[i], s['active'], cache=caches[i])
                            if not s.get('anchored') and obs['state'] in ('normal', 'activity_panel', 'claim_menu'):
                                panel_anchor = (obs['state'] == 'activity_panel'
                                                and text_hits(items, '日常活动', TABS)
                                                and text_hits(items, '活动', (.28, .07, .4, .11)))
                                if not panel_anchor and not any(text_hits(items, t, TOP) for t in ('活动', '排行', '指引')):
                                    s['paused'] = 'no game top-bar anchor; recalibrate rectangle'
                                else:
                                    s['anchored'] = True
                            if obs['state'] == 'battle' and obs['source'] != 'local_battle_anchors':
                                anchors[i].learn(image)
                            elif obs['state'] not in ('unknown', 'battle'):
                                anchors[i] = BattleAnchors()
                            action = advance(s, obs, time.monotonic(), wf, cfg, args.execute, rng)
                            s['observation'] = obs
                            confirmed = s.pop('confirmed', None)
                            if confirmed:
                                after = shot_name(win['name'], confirmed+'-after')
                                image.save(args.output/after)
                                event(window=win['name'], confirmed=confirmed, after=after, count=s['count'])
                            recovered = s.pop('recovered', None)
                            if recovered:
                                event(window=win['name'], recovered=recovered,
                                      count=s['count'], missed=s.get('missed_rounds', 0),
                                      spent=s.get('estimated_spent'))
                            if s.get('fresh'):
                                event(window=win['name'], observation=obs, count=s['count'],
                                      continue_delay=s.get('continue_delay'))
                            persist()
                            if args.execute and action and s.get('anchored'):
                                await execute_node(action, win, s, obs, caches[i])
                            save(args.output/f"recognition-{win['name']}.json", dict(observation=obs, items=items))
                            with (args.output/'timeline.jsonl').open('a') as f:
                                f.write(json.dumps(dict(time=time.time(), window=win['name'], **obs,
                                        count=s['count'], action=action, pending=s.get('pending'),
                                        paused=s.get('paused'), warning=s.get('warning')), ensure_ascii=False)+'\n')
                        except Exception as exc:
                            if stopping():
                                break
                            s['paused'] = str(exc)
                            event(window=win['name'], error=str(exc), pending=s.get('pending'))
                        persist()
                    summary['loop_seconds'] = round(time.monotonic()-started, 3)
                    if summary['loop_seconds'] > max(10, 7*len(states)):
                        raise RuntimeError('Loop exceeded observation budget; inspect latency')
                    if all(s.get('paused') or s['count'] >= s['target'] for s in states):
                        summary['status'] = 'complete' if all(s['count'] >= s['target'] for s in states) else 'paused'
                        break
                    await asyncio.sleep(max(.1, 1-(time.monotonic()-started)))
                if summary['status'] == 'running':
                    summary['status'] = 'stopped'
    except (Exception, BaseExceptionGroup) as exc:
        summary.update(status='stopped' if stopping() else 'paused', error=str(exc))
        event(error=str(exc))
    finally:
        persist()
        cleanup_frames(args.output)
        if viewer and viewer.poll() is None:
            viewer.terminate()
        for lock in locks:
            lock.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary['status'] == 'paused' else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--image', type=Path, required=True)
    inspect.add_argument('--rect', help='x,y,w,h content rectangle; reference images need title bar removed')
    live = sub.add_parser('run')
    live.add_argument('--repo', type=Path, required=True)
    live.add_argument('--config', type=Path, required=True)
    live.add_argument('--output', type=Path, required=True)
    live.add_argument('--execute', action='store_true')
    live.add_argument('--rounds', type=int, default=0, help='positive override, otherwise per-window plan')
    live.add_argument('--minutes', type=float, default=0)
    live.add_argument('--mcp-spec', type=Path)
    live.add_argument('--viewer', dest='viewer', action='store_true', default=True,
                      help='readonly web console (default on)')
    live.add_argument('--no-viewer', dest='viewer', action='store_false')
    live.add_argument('--viewer-port', type=int, default=8792)
    args = parser.parse_args()
    if args.command == 'inspect':
        image = Image.open(args.image).convert('RGB')
        if args.rect:
            x, y, w, h = map(int, args.rect.split(','))
            image = image.crop((x, y, x+w, y+h))
        obs, items = recognize(image, ZhuaguiWorkflow(build_screens(), []))
        print(json.dumps(dict(observation=obs, items=items), ensure_ascii=False, indent=2))
        return 0
    if not 0 <= args.rounds <= 50 or not math.isfinite(args.minutes) or args.minutes < 0:
        parser.error('invalid rounds or minutes')
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
