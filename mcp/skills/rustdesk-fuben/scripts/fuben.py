#!/usr/bin/env python3
"""Local Apple Vision/Pillow recognition and serial RustDesk MCP dungeon runs.

Knight dungeons first (3 on Monday, otherwise 2), then the two daily normal
dungeons. While a leader sits on the knight ready screen, the member clients
on other devices are readied automatically: a per-device sweep OCRs the full
frame, pairs each ready popup's 准备 button with its same-row 拒绝, and clicks
准备 (members on a leader device serialize with its loop; other devices
sweep in a parallel task). Requeue-on-countdown-cancel stays
as the safety net when no popup is found."""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
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
ACTIVITY = (.20, 0, .20, .12)
PANEL_HEAD = (.40, .09, .20, .08)
# 百晓仙子's menu slides with where the team stands (head seen at y .38 and
# .46 live); one wide right-column band covers both her menu and the
# in-dungeon dialog, told apart by the 选择副本 option.
MENU_BAND = (.66, .26, .34, .42)
BAI_HEAD = (.66, .26, .34, .42)
BAI_FIRST = (.70, .32, .27, .32)
DTAB_NORMAL = (.13, .18, .16, .09)
DTAB_KNIGHT = (.29, .18, .16, .09)
DPANEL_HEAD = (.68, .19, .30, .09)
DBUTTONS = (.14, .73, .56, .12)
WAIT_TXT = (.32, .72, .36, .15)
WAIT_ROW = (.08, .57, .90, .16)
DTASK_STEP = (.72, .24, .27, .09)
# The right-hand task panel slides left on narrower windows (676px live:
# skip button center measured down to ~.74; the reference 770px sat ~.80) —
# the band keeps only x widened, y stays put so panel titles stay out.
SKIP = (.70, .30, .29, .10)
DLG_HEAD = (.66, .26, .34, .42)
DLG_OPT = (.68, .60, .29, .16)
REWARD_TXT = (.36, .73, .28, .10)
ROUND = (.42, 0, .16, .12)
CANCEL = (.90, .80, .10, .20)
# Neutral scene spot: closes overlays; never sits on real UI.
DIALOG_TAP = (.26, .42, .07, .06)
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
        if not text_hits([it], text, box):
            continue
        cx, cy = it['x']+it['w']/2, it['y']+it['h']/2
        if not any(abs(cx-(h['x']+h['w']/2)) < .025
                   and abs(cy-(h['y']+h['h']/2)) < .02 for h in hits):
            hits.append(it)
    return hits[0] if len(hits) == 1 else None


def wildcard_row(items):
    """Row whose activity name contains '-普通' (the dungeon pair rotates
    daily) bound to its same-row, same-lane 参加 button. A row already done
    shows 已完成 instead of 参加 and is skipped naturally."""
    for it in items:
        t = re.sub(r'\s', '', it['text'])
        if '-普通' not in t or not text_hits([it], t, LIST):
            continue
        cx, cy = it['x']+it['w']/2, it['y']+it['h']/2
        lane = (.45, .64) if cx < .60 else (.82, .94)
        hits = [b for b in items if text_hits([b], '参加', LIST)
                and lane[0] <= b['x']+b['w']/2 <= lane[1]
                and abs(b['y']+b['h']/2-cy) < .065]
        btn = unique_text(hits, '参加', LIST) if hits else None
        if btn is not None:
            return it, btn
    return None, None


def battle_hits(items):
    toolbar = (.55, .5, .45, .5)
    if sum(text_hits(items, t, toolbar) for t in ('法术', '攻击', '道具', '防御', '自动')) >= 3:
        return True
    digit = any(it['text'].isdigit() and text_hits([it], it['text'], ROUND, .25)
                for it in items)
    return digit and text_hits(items, '取消', CANCEL, .5)


def build_screens():
    return [
        Screen('knight_wait', [[('正在等待', WAIT_TXT)], [('倒计时', WAIT_TXT)]]),
        Screen('reward', [[('点击屏幕继续', REWARD_TXT)]]),
        Screen('dungeon_panel', [[('侠士副本', DTAB_KNIGHT), ('普通副本', DTAB_NORMAL)]]),
        Screen('baixiao_menu', [[('请选择要做的事', BAI_HEAD), ('选择副本', MENU_BAND)]]),
        Screen('npc_dialog', [[('请选择要做的事', DLG_HEAD)]],
               forbid=[('选择副本', MENU_BAND), ('侠士副本', DTAB_KNIGHT)]),
        Screen('activity_panel', [[('活动', PANEL_HEAD), ('日常活动', TABS)]]),
        Screen('dungeon_done', [[('副本已完成', DTASK_STEP)]]),
        Screen('normal', [[('活动', ACTIVITY)]],
               forbid=[('侠士副本', DTAB_KNIGHT), ('请选择要做的事', BAI_HEAD),
                       ('请选择要做的事', DLG_HEAD), ('日常活动', TABS),
                       ('副本已完成', DTASK_STEP), ('点击屏幕继续', REWARD_TXT),
                       ('取消', CANCEL)]),
        # Dungeon maps hide the top bar; the 时辰 coordinate line is the
        # stable "we are on a map" mark there (city screens match 'normal'
        # first because they keep the 活动 button).
        Screen('dungeon_map', [[('时', MAP)]],
               forbid=[('侠士副本', DTAB_KNIGHT), ('请选择要做的事', BAI_HEAD),
                       ('请选择要做的事', DLG_HEAD), ('日常活动', TABS),
                       ('副本已完成', DTASK_STEP), ('点击屏幕继续', REWARD_TXT),
                       ('取消', CANCEL)]),
    ]


class FubenWorkflow(Workflow):
    def classify(self, items):
        # Foreground overlays first, then battle, then the ordered screens.
        if self.screens[0].match(items):
            return 'knight_wait'
        if self.screens[1].match(items):
            return 'reward'
        if battle_hits(items):
            return 'battle'
        return super().classify(items)


def build_workflow(cfg):
    return FubenWorkflow(build_screens(), [
        Node('open_activity', 'normal', ClickText('活动', ACTIVITY), ['activity_panel'], 20),
        Node('select_daily', 'activity_panel', ClickText('日常活动', TABS), ['activity_panel'], 20),
        Node('join_dungeon', 'activity_panel', ClickEntry('-普通', '参加', LIST,
             cfg.get('max_scrolls', 8), tuple(cfg['activity_list_scroll'])), ['baixiao_menu'], 180),
        Node('open_dungeon_menu', 'baixiao_menu', ClickText('选择副本', BAI_FIRST),
             ['dungeon_panel'], 30),
        Node('pick_knight_tab', 'dungeon_panel', ClickText('侠士副本', DTAB_KNIGHT),
             ['dungeon_panel'], 20),
        Node('pick_normal_tab', 'dungeon_panel', ClickText('普通副本', DTAB_NORMAL),
             ['dungeon_panel'], 20),
        Node('enter_dungeon', 'dungeon_panel', ClickText('进入', DBUTTONS),
             ['knight_wait', 'dungeon_map'], 60),
        Node('skip_story', 'dungeon_map', ClickText('跳过剧情', SKIP),
             ['dungeon_map', 'npc_dialog', 'battle', 'reward', 'dungeon_done'], 40),
        Node('advance_task', 'dungeon_map', ClickPoint(TASK),
             ['npc_dialog', 'battle', 'dungeon_map', 'reward', 'dungeon_done'], 90),
        Node('dialog_confirm', 'npc_dialog', ClickPoint(DLG_OPT),
             ['battle', 'dungeon_map', 'reward', 'dungeon_done'], 60),
        Node('close_reward', 'reward', ClickText('点击屏幕继续', REWARD_TXT),
             ['dungeon_done', 'dungeon_map'], 30),
        Node('finish_dungeon', 'dungeon_done', ClickText('副本已完成', DTASK_STEP),
             ['normal', 'dungeon_map', 'activity_panel', 'baixiao_menu'], 60),
        Node('dismiss_overlay', 'unknown', ClickPoint(DIALOG_TAP),
             ['normal', 'dungeon_map', 'activity_panel', 'baixiao_menu',
              'dungeon_panel', 'npc_dialog', 'battle', 'knight_wait', 'reward',
              'dungeon_done', 'unknown'], 20),
    ])


def task_row(items):
    """The in-dungeon task entry: the step line if readable, else the title."""
    for box in (DTASK_STEP, TASK):
        best = None
        for it in items:
            t = re.sub(r'\s', '', it['text'])
            if (len(t) >= 2 and it['score'] >= .3
                    and text_hits([it], it['text'], box)
                    and not any(s in t for s in ('任务', '队伍'))):
                if best is None or it['score'] > best['score']:
                    best = it
        if best is not None:
            return best
    return None


def enter_button(items, avoid_x=None):
    """Leftmost available 进入 card button. A finished normal card keeps its
    yellow 进入 (only a stamp on the art changes), so repeats are excluded
    positionally: avoid_x drops the column of the previous enter."""
    hits = [it for it in items if text_hits([it], '进入', DBUTTONS)]
    if avoid_x is not None:
        hits = [it for it in hits if abs((it['x']+it['w']/2)-avoid_x) > .06]
    return min(hits, key=lambda it: it['x']) if hits else None


def ready_buttons(items):
    """The member ready popup's 准备 buttons in a full-device OCR pass. A
    button is an item carrying a '准?/淮?' two-char read paired with a
    same-row '拒绝' on its left: the popup's own member list (未准备 rows)
    never pairs, and the right-of-拒绝 ordering keeps a misread from
    drifting onto 拒绝 itself. Small windows OCR the button as 准留/准盆/
    淮备 (measured against 7.png at 615px window width) — the first glyph
    degrades to at worst 淮, the pair rule stays the discriminator."""
    def strip(t):
        return re.sub(r'\s', '', t)
    rejects = [it for it in items if '拒绝' in strip(it['text']) and it['score'] >= .25]
    buttons = []
    for it in items:
        t = strip(it['text'])
        if it['score'] < .25 or not any(t[i] in '准淮' and i+1 < len(t)
                                        for i in range(len(t)-1)):
            continue
        cy, cx = it['y']+it['h']/2, it['x']+it['w']/2
        for r in rejects:
            rx, ry = r['x']+r['w']/2, r['y']+r['h']/2
            if (rx < cx and .03 < cx-rx < .45
                    and abs(cy-ry) <= max(it['h'], r['h'])*.6):
                buttons.append(it)
                break
    buttons.sort(key=lambda it: it['x'])
    uniq = []
    for b in buttons:
        if not any(abs(b['x']+b['w']/2-(u['x']+u['w']/2)) < .02
                   and abs(b['y']+b['h']/2-(u['y']+u['h']/2)) < .03 for u in uniq):
            uniq.append(b)
    return uniq


def reject_bands(items):
    """Horizontal bands around every 拒绝 read — a 2x re-read of these
    rescues the button text when a small window's 准备 OCRs as 准留/准盆."""
    bands = []
    for it in items:
        if '拒绝' not in re.sub(r'\s', '', it['text']) or it['score'] < .25:
            continue
        cy = it['y']+it['h']/2
        x0, y0 = max(0., it['x']-.30), max(0., cy-.035)
        bands.append((x0, y0, min(.60, 1-x0), it['h']+.07))
    return bands


def detect_ready_popups(image):
    """The sweep's full OCR cascade over one member frame: a plain pass,
    then 2x re-reads of the 拒绝 bands when the button text went missing
    (small windows). Returns (buttons, items)."""
    items = ocr(image)
    buttons = ready_buttons(items)
    if not buttons:
        zoomed = list(items)
        for band in reject_bands(items):
            zoomed += region_ocr(image, band, 2)
        buttons = ready_buttons(zoomed)
    return buttons, items


def dialog_option(items):
    """The dialog's single option, anchored just below its '请选择要做的事'
    prompt row — the prompt itself and off-band chatter never match."""
    prompt = None
    for it in items:
        if it['score'] >= .3 and '选择要做的事' in re.sub(r'\s', '', it['text']):
            prompt = it
            break
    if prompt is None:
        return None
    pb = prompt['y']+prompt['h']
    best = None
    for it in items:
        t = re.sub(r'\s', '', it['text'])
        cy = it['y']+it['h']/2
        if (len(t) >= 3 and it['score'] >= .4 and '选择要做的事' not in t
                and pb+.03 <= cy <= pb+.18 and it['x']+it['w']/2 >= .64
                and it['w'] >= .03):
            if best is None or it['score'] > best['score']:
                best = it
    return best


def map_position(items):
    for it in items:
        if it['score'] < .25 or not text_hits([it], it['text'], MAP):
            continue
        # Vision often reads the small-font full-width comma as '.'/'、'.
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
    x = max(0, min(round(sum(est_x)/len(est_x)), W-100))
    y = max(0, min(round(sum(est_y)/len(est_y)), H-100))
    return [x, y, w, h]


def titlebar_runs(img, y_hint=None):
    """Segment a title-bar strip into window spans across the wallpaper gaps:
    uniform saturated columns are wallpaper, everything else is a window.
    Without y_hint the strip is the screen top (windows pinned there); with
    it, the strip sits just above the configured content top — with four
    tiled windows the screen-top strip belongs to the upper row, not ours.
    Returns [(x_start, x_end, content_top)]."""
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


def topbar_strip_items(img, y_hint):
    """OCR the full-width top strip around `y_hint`, upscaled 2x — the labels
    are ~20px tall and a plain full-frame pass misses them."""
    W, H = img.size
    top = max(0, round(y_hint)-6)
    height = min(H-top, 110)
    return region_ocr(img, (0, top/H, 1, (top+height)/H), 2)


def recognize(img, wf, anchors=None, active=False, cache=None):
    started = time.monotonic()
    read = cache.read if cache is not None else region_ocr
    items = []
    for box in (WAIT_TXT, PANEL_HEAD, BAI_HEAD, BAI_FIRST, DTAB_KNIGHT, DTAB_NORMAL,
                DPANEL_HEAD, DBUTTONS, DLG_HEAD, DLG_OPT, REWARD_TXT, DTASK_STEP,
                SKIP, TASK, ROUND, CANCEL, TOP):
        items += read(img, box)
    source = 'roi_ocr'
    state = wf.classify(items)
    if state in ('unknown', 'dungeon_map', 'npc_dialog'):
        items += read(img, MAP, 3)
        items += read(img, DTASK_STEP, 2)
        items += read(img, DLG_OPT, 2)
        state = wf.classify(items)
    if state == 'activity_panel' or text_hits(items, '活动', PANEL_HEAD):
        items += read(img, TABS, 2)
        items += read(img, LIST, 2)
        state = wf.classify(items)
    if state == 'dungeon_panel':
        # The 进入 row is small; a plain sweep misses it on some frames.
        items += read(img, DBUTTONS, 2)
        items += read(img, (.05, .15, .55, .15), 2)
        state = wf.classify(items)
    if state == 'dungeon_map':
        # City frames occasionally lose the top-bar 活动 on the first pass;
        # an upscaled re-read lets 'normal' (ordered earlier) win back.
        items += read(img, (.15, 0, .32, .13), 2)
        state = wf.classify(items)
    if state == 'unknown':
        items += ocr(img)
        state = wf.classify(items)
        source = 'full_ocr_fallback'
    if state == 'unknown' and active and anchors is not None and anchors.matches(img):
        # Never maintain battle over an unresolved foreground overlay.
        if not any(text_hits(items, t, b) for t, b in (
                ('跳过剧情', SKIP), ('请选择要做的事', DLG_HEAD), ('请选择要做的事', BAI_HEAD))):
            state, source = 'battle', 'local_battle_anchors'
    return dict(state=state, source=source, map_position=map_position(items),
                recognition_seconds=round(time.monotonic()-started, 4)), items


def default_knight():
    """Monday needs a third knight run (the weekly first pays no 侠义值)."""
    return 3 if datetime.date.today().weekday() == 0 else 2


def make_state(knight_target, normal_target):
    return dict(knight=0, knight_target=knight_target,
                normal=0, normal_target=normal_target,
                run_open=False, entering=None, tab_pending=None,
                enter_attempts=0, battle_seen=False, initialized=False,
                active=False)


def cleanup_frames(output):
    for path in output.glob('*.png'):
        if not path.name.endswith(('-before.png', '-after.png')):
            path.unlink()


def begin_action(s, action, now):
    s.update(pending=action, sent=now)
    if action == 'open_activity':
        s['daily_selected'] = False
    elif action == 'join_dungeon':
        s['join_attempts'] = s.get('join_attempts', 0)+1
    elif action == 'enter_dungeon':
        s['enter_attempts'] = s.get('enter_attempts', 0)+1
    elif action in ('skip_story', 'advance_task', 'dialog_confirm'):
        s['step_sent'] = now
    elif action == 'finish_dungeon':
        s['finishing'] = True


def next_target(s):
    """knight first, then the normal pair; None when the window is done."""
    if s['knight'] < s['knight_target']:
        return 'knight'
    if s['normal'] < s['normal_target']:
        return 'normal'
    return None


def rng_uniform(rng, lo, hi):
    return (rng if rng is not None else random).uniform(lo, hi)


def open_run(s, now):
    s.update(run_open=True, run_since=now, battle_seen=False,
             tab_pending=None, finishing=None)
    s.pop('retry_at', None)
    s.pop('step_sent', None)
    s.pop('city_since', None)
    s.pop('confirm_extends', None)


def close_run(s, now):
    """Count the finished dungeon and reset the between-run bookkeeping."""
    kind = s.get('entering') or next_target(s) or 'knight'
    s[kind] = s.get(kind, 0)+1
    s.update(run_open=False, entering=None, battle_seen=False, finishing=None)
    s.pop('run_since', None)
    s.pop('retry_at', None)
    s.pop('step_sent', None)


def advance(s, obs, items, now, wf, cfg, execute=True, rng=None):
    """Drive one window. Every input is gated on two-frame evidence and a
    pending confirmation; the ready screen only ever waits."""
    state = obs['state']
    if s.get('paused') or next_target(s) is None:
        return None
    previous = s.get('seen')
    s['streak'] = s.get('streak', 0)+1 if previous == state else 1
    s['seen'] = state
    s['fresh'] = previous != state
    if not s['initialized']:
        s.setdefault('init_since', now)
        if state in ('normal', 'activity_panel', 'baixiao_menu', 'dungeon_panel'):
            s['initialized'] = True
        elif state in ('npc_dialog', 'knight_wait', 'reward', 'dungeon_done', 'dungeon_map'):
            s['initialized'] = True
            if execute:
                s.update(entering=next_target(s))
                open_run(s, now)
                s['recovered'] = 'adopted_open_dungeon'
        elif state == 'battle':
            s['initialized'] = True
            if execute:
                s.update(entering=next_target(s))
                open_run(s, now)
                s['battle_seen'] = True
                s['recovered'] = 'adopted_open_dungeon'
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
    pos = obs.get('map_position')
    moved = pos is not None and s.get('position') is not None and pos != s['position']
    if pos is not None:
        if moved or s.get('position') is None:
            s['pos_since'] = now
        s['position'] = pos
    if state == 'battle':
        s['active'] = True
        s['battle_seen'] = True
    elif state != 'unknown':
        s['active'] = False
    pending = s.get('pending')
    if pending:
        node = wf.nodes[pending]
        if state in node.expect:
            s['confirmed'] = pending
            s.pop('pending', None)
            s.pop('sent', None)
            s.pop('confirm_extends', None)
            if pending == 'select_daily':
                s['daily_selected'] = True
            if pending == 'join_dungeon':
                s.pop('retab_tries', None)   # fresh tab budget next round
            if pending == 'enter_dungeon':
                open_run(s, now)
                s['entering'] = s.get('entering') or next_target(s)
                s['enter_attempts'] = 0   # a fresh queue budget per dungeon
                s['enter_none'] = 0
        elif pending in ('enter_dungeon', 'join_dungeon') and state == node.source:
            budget = (('join_attempts', 'max_join_retries') if pending == 'join_dungeon'
                      else ('enter_attempts', 'max_enter_retries'))
            # A swallowed click (observed 2026-10-04: cursor fought by a
            # human hand) leaves the source screen unchanged; re-click well
            # before the node timeout. The retry budget bounds the total.
            if now-s['sent'] > 12 and s.get(budget[0], 0) < cfg.get(budget[1], 3):
                s.pop('pending', None)
                s.pop('sent', None)
                return pending
        elif now-s['sent'] > node.timeout:
            if (pending in ('open_activity', 'select_daily') and s.get('nav_fails', 0) < 3
                    and state != 'battle' and not s.get('active')):
                # An overlay swallowed the click (leaderboard, bag…): close it
                # with a right-click, then line up again.
                s['nav_fails'] = s.get('nav_fails', 0)+1
                s.pop('pending', None)
                s.pop('sent', None)
                return 'dismiss_overlay'
            if pending == 'join_dungeon' and s.get('join_attempts', 0) < cfg.get('max_join_retries', 3):
                # A missed 参加 click (list drifted, walk interrupted): line
                # up again instead of freezing.
                s.pop('pending', None)
                s.pop('sent', None)
                s['retry_at'] = now+rng_uniform(rng, *cfg.get('enter_retry_delay', (8, 20)))
            elif pending == 'enter_dungeon' and s.get('enter_attempts', 0) < cfg.get('max_enter_retries', 3):
                # The ready countdown cancels un-entered requests; the member
                # sweep may have missed a popup, so line up again after a
                # beat (the popups reappear for the next queue).
                s.pop('pending', None)
                s.pop('sent', None)
                s['retry_at'] = now+rng_uniform(rng, *cfg.get('enter_retry_delay', (8, 20)))
            elif pending == 'enter_dungeon':
                s['paused'] = ('knight requeue gave up after %d tries; members not readied'
                               % s.get('enter_attempts', 0))
            elif pending in ('skip_story', 'dialog_confirm') and state != node.source:
                # The click landed and the beat is mid-transition through a
                # screen the classifier cannot name — knight story cutscenes
                # read unknown for well over a minute (live 2026-10-05:
                # dialog_confirm paused mid-cutscene after doing its job).
                # Keep waiting while the screen has moved off the source; a
                # truly swallowed click stays on the source and pauses.
                s['confirm_extends'] = s.get('confirm_extends', 0)+1
                if s['confirm_extends'] <= 6:
                    s['sent'] = now   # re-arm the clock; ~6 min of cutscene
                else:
                    s['paused'] = 'action not confirmed: '+pending
            else:
                s['paused'] = 'action not confirmed: '+pending
        return None
    if state == 'battle':
        s['active'] = True
        s['battle_seen'] = True
    elif state != 'unknown':
        s['active'] = False
    if state == 'unknown':
        s.setdefault('unknown_since', now)
        if now-s['unknown_since'] > 20:
            s['warning'] = 'unknown screen; inputs withheld'
        # A stray panel (leaderboard, bag, …) that nothing drives: right-click
        # closes most game overlays — try it instead of stalling forever.
        if now-s['unknown_since'] > cfg.get('overlay_grace', 30) and not s.get('active'):
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
    if s['streak'] < 2:
        return None
    # ---- inside a run: screens drive the skip/navigate/dialog cycle ----
    if s['run_open']:
        if state == 'knight_wait':
            return None  # the member sweep readies the members; waiting is the job
        if state == 'reward':
            return 'close_reward'
        if state == 'dungeon_done':
            if not s.get('finishing'):
                close_run(s, now)   # gray box sighting completes the run
            return 'finish_dungeon'
        if state == 'npc_dialog':
            # The screen signature IS the gate (prompt 请选择要做的事). The
            # option text rotates with the story (住手！/不行！/好重的杀气…)
            # and is clicked geometrically — never a text gate.
            return 'dialog_confirm'
        if state == 'normal':
            # The finish click teleports to the city; if the gray box was
            # missed entirely, a sustained city view still closes the run.
            s.setdefault('city_since', now)
            if now-s['city_since'] > cfg.get('assist_grace', 60):
                s['recovered'] = 'missed_dungeon_end'
                close_run(s, now)
            return None
        if state == 'dungeon_map':
            if text_hits(items, '跳过剧情', SKIP):
                return 'skip_story'
            delay = cfg.get('navigation_delay', 3)
            if s.get('step_sent') is not None and now-s['step_sent'] < delay:
                return None
            if moved or (s.get('pos_since') is not None and now-s['pos_since'] < delay):
                return None
            if task_row(items) is not None:
                return 'advance_task'
            if now-s.get('run_since', now) > cfg.get('navigation_timeout', 300):
                s['warning'] = 'no dungeon progress on the map'
            return None
        if state == 'dungeon_panel' and s.get('entering') == 'knight':
            # Bounced back after a failed ready countdown: line up again,
            # but stop spinning when the member sweep keeps failing.
            if s.get('enter_attempts', 0) >= cfg.get('max_enter_retries', 3):
                s['paused'] = ('knight requeue gave up after %d tries; members not readied'
                               % s.get('enter_attempts', 0))
                return None
            if s.get('retry_at') is not None and now < s['retry_at']:
                return None
            s.pop('retry_at', None)
            s['tab_pending'] = 'knight'
            return 'enter_dungeon'
        return None
    # ---- between runs: walk the panel path toward the next target ----
    if state in ('dungeon_map', 'battle', 'reward', 'dungeon_done'):
        # The game auto-entered an earlier queue (members readied while we
        # were re-navigating; live 2026-10-05: the right leader queued, the
        # sweep readied its team, the countdown entered without our own
        # 进入 click) — adopt the open dungeon instead of wandering the
        # panel path forever.
        s.update(entering=next_target(s))
        open_run(s, now)
        if state == 'battle':
            s['battle_seen'] = True
        s['recovered'] = 'adopted_open_dungeon'
        return None
    if state == 'normal':
        return 'open_activity'
    if state == 'activity_panel':
        if s.get('retry_at') is not None and now < s['retry_at']:
            return None
        s.pop('retry_at', None)
        if not s.get('daily_selected'):
            return 'select_daily'
        if (not any('-普通' in re.sub(r'\s', '', it['text']) for it in items)
                and s.get('retab_tries', 0) < 2):
            # The panel sometimes opens on the wrong tab or the tab click is
            # swallowed (run4 right: a full 80-item panel read with zero
            # -普通 rows). Re-selecting 日常活动 is idempotent and resets the
            # list scroll to the top; hunt from there before pausing.
            s['retab_tries'] = s.get('retab_tries', 0)+1
            return 'select_daily'
        return 'join_dungeon'
    if state == 'baixiao_menu':
        return 'open_dungeon_menu'
    if state == 'dungeon_panel':
        if s.get('retry_at') is not None and now < s['retry_at']:
            return None
        s.pop('retry_at', None)
        if s.get('tab_pending'):
            return 'enter_dungeon'
        target = next_target(s)
        s['tab_pending'] = target
        s['entering'] = target
        return 'pick_knight_tab' if target == 'knight' else 'pick_normal_tab'
    return None


def validate(cfg):
    if type(cfg.get('ocr_cache', True)) is not bool:
        raise ValueError('ocr_cache must be boolean')
    if not cfg.get('calibrated'):
        raise ValueError('Requires freshly calibrated configuration')
    res = cfg.get('native_resolution')
    if not isinstance(res, list) or len(res) != 2 or any(type(v) is not int or v < 1 for v in res):
        raise ValueError('Invalid native_resolution')
    windows = cfg.get('windows', [])
    if not 1 <= len(windows) <= 2 or len({w['name'] for w in windows}) != len(windows):
        raise ValueError('Requires one or two distinct windows')
    rects = []
    for win in windows:
        if not re.fullmatch(r'[a-z0-9_-]+', win['name']) or not win.get('identity'):
            raise ValueError('Missing calibrated window identity or invalid name')
        dev = win.get('device', cfg.get('device'))
        if not isinstance(dev, str) or not re.fullmatch(r'[a-z0-9_-]+', dev):
            raise ValueError('Requires a valid device name')
        rect = win.get('rect')
        if not isinstance(rect, list) or len(rect) != 4 or any(type(v) is not int for v in rect):
            raise ValueError('Invalid window rect')
        x, y, w, h = rect
        if min(x, y) < 0 or not 300 <= w <= 2560 or h < 300 or x+w > res[0] or y+h > res[1]:
            raise ValueError('Invalid content rectangle')
        for other_dev, other in rects:
            if dev == other_dev and max(x, other[0]) < min(x+w, other[0]+other[2]) and max(y, other[1]) < min(y+h, other[1]+other[3]):
                raise ValueError('Window content rectangles overlap')
        rects.append((dev, rect))
        kt = win.get('knight_runs', cfg.get('knight_runs'))
        if kt is not None and (type(kt) is not int or not 0 <= kt <= 9):
            raise ValueError('knight_runs must be an integer in 0..9')
        nt = win.get('normal_runs', cfg.get('normal_runs', 2))
        if type(nt) is not int or not 0 <= nt <= 3:
            raise ValueError('normal_runs must be an integer in 0..3')
    box = cfg.get('activity_list_scroll', [.65, .25, .22, .32])
    if len(box) != 4 or any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 or v > 1 for v in box) or min(box[2:]) <= 0 or box[0]+box[2] > 1 or box[1]+box[3] > 1:
        raise ValueError('Invalid activity_list_scroll')
    cfg['activity_list_scroll'] = box
    members = cfg.get('members', [])
    if not isinstance(members, list):
        raise ValueError('members must be a list of device names')
    for dev in members:
        if not isinstance(dev, str) or not re.fullmatch(r'[a-z0-9_-]+', dev):
            raise ValueError('Invalid member device name: '+str(dev))
    if len(set(members)) != len(members):
        raise ValueError('member devices must be distinct')
    cfg['members'] = list(members)
    # Members may share the leader device: the sweep is a rect-free full-frame
    # popup scan, so the other windows on that device need no configuration
    # (single-connection layout: everything on one screen).
    resweep = cfg.get('member_resweep', (1.5, 3.0))
    if (not isinstance(resweep, (list, tuple)) or len(resweep) != 2
            or any(type(v) not in (float, int) or not math.isfinite(v) or not 0 < v <= 60 for v in resweep)
            or resweep[0] > resweep[1]):
        raise ValueError('member_resweep must be two ascending seconds in (0, 60]')
    cfg['member_resweep'] = [float(resweep[0]), float(resweep[1])]
    for key in ('navigation_delay', 'navigation_timeout', 'max_enter_retries', 'max_join_retries'):
        default = dict(navigation_delay=3, navigation_timeout=300, max_enter_retries=3,
                       max_join_retries=3)[key]
        val = cfg.get(key, default)
        if type(val) not in (float, int) or not math.isfinite(val) or val <= 0:
            raise ValueError('Invalid '+key)
    if type(cfg.get('max_scrolls', 8)) is not int or not 2 <= cfg.get('max_scrolls', 8) <= 20:
        raise ValueError('max_scrolls must be 2..20')
    return [(win.get('knight_runs', cfg.get('knight_runs', default_knight())),
             win.get('normal_runs', cfg.get('normal_runs', 2)))
            for win in windows]


async def run(args):
    cfg = json.loads(args.config.read_text())
    plans = validate(cfg)
    wf = build_workflow(cfg)
    spec = load_spec(args.mcp_spec, args.repo.resolve())
    devices = sorted({w.get('device', cfg.get('device')) for w in cfg['windows']})
    member_devs = list(cfg.get('members', []))
    # Members on a leader device must not interrupt its cursor travel.
    shared_members = [d for d in member_devs if d in devices]
    other_members = [d for d in member_devs if d not in shared_members]
    all_devs = sorted(set(devices) | set(member_devs))
    locks = []
    # Shared and legacy activity locks.
    lock_devs = set(all_devs)
    try:
        for dev in sorted(lock_devs):
            for tag in ('device', 'jianhui', 'menpai', 'mijing', 'zhuagui', 'fuben'):
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
                   games=max(p[0]+p[1] for p in plans), config=cfg, windows=states,
                   members={dev: dict(clicked=0) for dev in member_devs})
    event = event_logger(args.output)
    stopped = False
    viewer = None
    worker = None
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
        Configured rectangles are size hints, never trusted positions."""
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
                span = min(spans, key=lambda sp: abs((sp[0]+sp[1])/2-(x0+w/2)))
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
                snapped[2] = W-snapped[0]
            if snapped[1]+snapped[3] > H:
                snapped[3] = H-snapped[1]
            win['rect'] = snapped
            event(window=win['name'], autocal_source=source)
            event(window=win['name'], autocalibrated=snapped)
        persist()

    rng = random.Random(cfg.get('rng_seed'))
    planner, scroller = ClickPlanner(rng), ScrollPlanner(rng, segments=(2, 3), notches=(1, 2))

    member_next = {dev: 0.0 for dev in member_devs}
    member_native = {}
    waiting = {'now': False}   # any leader currently on the knight ready screen

    async def member_frame(dev):
        content = await call('screenshot', dev, format='png', max_width=2560,
                             wait_for_new_frame=True)
        note = ' '.join(c.text for c in content if c.type == 'text')
        dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
        if not dims:
            raise RuntimeError('member sweep: no native resolution in note')
        if 'stale=True' in note:
            raise RuntimeError('Stale screenshot; no input allowed')
        blob = next(c.data for c in content if c.type == 'image')
        image = Image.open(io.BytesIO(base64.b64decode(blob))).convert('RGB')
        if image.size != tuple(map(int, dims.groups())):
            raise RuntimeError('member frame downscaled; raise max_width')
        member_native[dev] = list(map(int, dims.groups()))
        return image

    async def sweep_members(dev):
        """One readiness pass over one member device: OCR the full frame,
        pair every ready popup's 准备 with its 拒绝, and click. The frame is
        re-checked after cursor travel — the countdown can kill a popup
        mid-sweep and the bare city behind it must never take the click."""
        image = await member_frame(dev)
        buttons, _ = await asyncio.to_thread(detect_ready_popups, image)
        if not buttons:
            return 0
        if not args.execute:
            event(device=dev, member_popup=len(buttons), observe=True)
            return 0
        W, H = member_native[dev]
        clicked = 0
        for b in buttons:
            px, py = planner.point('member:'+dev,
                                   tuple(b[k] for k in ('x', 'y', 'w', 'h')), (W, H))
            before = shot_name(dev, 'member-ready-before')
            image.save(args.output/before)
            await call('mouse_move', dev, x=px, y=py)
            moved = await member_frame(dev)
            still, _ = await asyncio.to_thread(detect_ready_popups, moved)
            if not still:
                moved.save(args.output/shot_name(dev, 'member-ready-after'))
                event(device=dev, member_ready='popup gone before click', before=before)
                return clicked
            if not any(it['x']*W <= px <= (it['x']+it['w'])*W
                       and it['y']*H <= py <= (it['y']+it['h'])*H for it in still):
                moved.save(args.output/shot_name(dev, 'member-ready-after'))
                event(device=dev, member_ready='target drifted; retry next sweep', before=before)
                return clicked
            event(device=dev, intent='member_ready', before=before,
                  box=[round(b[k]*W) for k in ('x', 'y', 'w', 'h')])
            persist()
            await call('click', dev, x=px, y=py, humanize=True)
            event(device=dev, sent='member_ready')
            clicked += 1
            summary['members'][dev]['clicked'] += 1
            after = await member_frame(dev)
            after.save(args.output/shot_name(dev, 'member-ready-after'))
        return clicked

    def cool_member(dev):
        member_next[dev] = time.monotonic()+rng_uniform(rng, *cfg['member_resweep'])

    async def member_worker():
        while not stopping():
            if not waiting['now']:
                await asyncio.sleep(1)
                continue
            for dev in other_members:
                if stopping():
                    break
                if time.monotonic() < member_next.get(dev, 0):
                    continue
                try:
                    await sweep_members(dev)
                except Exception as exc:
                    event(device=dev, member_error=str(exc))
                cool_member(dev)
            await asyncio.sleep(.5)


    async def execute_node(action, win, s, obs, items, cache):
        node = wf.nodes[action]
        await asyncio.sleep(min(planner.delay(s.get('fresh', False)), 1))
        image = await capture(win)
        check, check_items = await asyncio.to_thread(recognize, image, wf, cache=cache)
        if check['state'] != obs['state']:
            event(window=win['name'], action_aborted=action, observed=check['state'])
            return
        target = None
        if action == 'join_dungeon':
            entry, target = wildcard_row(check_items)
            if target is None:
                if entry is not None or find_text(check_items, '-普通', LIST) is not None:
                    # Done rows show 已完成; only a scroll can find a live one.
                    attempt = s.get('entry_attempts', 0)
                    if attempt >= cfg.get('max_scrolls', 8):
                        s['paused'] = 'no live -普通 dungeon entry after bounded search'
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
                s['paused'] = 'no -普通 dungeon entry visible'
                return
        elif action == 'advance_task':
            target = task_row(check_items)
            if target is None:
                s['paused'] = 'no dungeon task row to click'
                return
        elif action == 'dialog_confirm':
            # Best-effort anchor only: the option row's position under the
            # prompt is fixed; its text rotates and is never trusted.
            target = dialog_option(check_items)
        elif action == 'enter_dungeon':
            # Normal runs must walk DISTINCT dungeons: a finished card keeps
            # a clickable 进入, so exclude the previous enter's column.
            avoid = s.get('last_enter_x') if s.get('entering') == 'normal' else None
            target = enter_button(check_items, avoid)
            if target is None:
                # A flaky read of the button row retries instead of freezing;
                # genuinely exhausted panels still surface as a pause.
                attempt = s.get('enter_none', 0)
                if attempt >= 3:
                    s['paused'] = ('no distinct enterable dungeon card '
                                   '(all done or locked?)')
                    return
                s['enter_none'] = attempt+1
                s['retry_at'] = time.monotonic()+rng_uniform(rng, 5, 12)
                event(window=win['name'], action_deferred=action,
                      reason='no enterable card read', attempt=attempt+1)
                return
        elif isinstance(node.act, ClickText):
            target = unique_text(check_items, node.act.text, node.act.box)
        fixed = isinstance(node.act, ClickPoint) or action in (
            'advance_task', 'join_dungeon', 'enter_dungeon', 'dialog_confirm')
        if target is None and not fixed:
            s['paused'] = 'missing or ambiguous click target: '+action
            return
        x0, y0, w, h = win['rect']
        # Always click the resolved OCR target when one exists; a node's
        # ClickPoint box is only a shape hint for item-based finders.
        box = (tuple(target[k] for k in ('x', 'y', 'w', 'h')) if target is not None
               else tuple(node.act.box))
        if action == 'open_activity' and target is not None:
            # 活动/排行 are adjacent and OCR often merges them into one wide
            # item; sampling the left half keeps the click off the leaderboard.
            box = (box[0], box[1], box[2]*.45, box[3])
        px, py = planner.point(win['name']+':'+action, box, (w, h))
        before = shot_name(win['name'], action+'-before')
        image.save(args.output/before)
        await call('mouse_move', dev_of(win), x=x0+px, y=y0+py)
        if action == 'dismiss_overlay':
            # Right-click closes most game overlays (leaderboard, bag, …).
            begin_action(s, action, time.monotonic())
            event(window=win['name'], intent=action, before=before,
                  knight=s['knight'], normal=s['normal'])
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
            moved_target = (wildcard_row(moved_items)[1] if action == 'join_dungeon'
                            else task_row(moved_items) if action == 'advance_task'
                            else dialog_option(moved_items) if action == 'dialog_confirm'
                            else enter_button(moved_items) if action == 'enter_dungeon'
                            else unique_text(moved_items, node.act.text, node.act.box))
            if (moved_obs['state'] != check['state'] or moved_target is None
                    or not (moved_target['x']*w <= px <= (moved_target['x']+moved_target['w'])*w
                            and moved_target['y']*h <= py <= (moved_target['y']+moved_target['h'])*h)):
                moved_image.save(args.output/shot_name(win['name'], action+'-after'))
                event(window=win['name'], action_aborted=action, reason='target changed after cursor travel')
                return
        begin_action(s, action, time.monotonic())
        event(window=win['name'], intent=action, before=before,
              knight=s['knight'], normal=s['normal'])
        persist()  # Uncertain delivery remains visible even if this call fails.
        await call('click', dev_of(win), x=x0+px, y=y0+py, humanize=True)
        event(window=win['name'], sent=action)
        if action == 'enter_dungeon' and s.get('entering') == 'normal':
            s['last_enter_x'] = box[0]+box[2]/2   # next normal enters elsewhere
            persist()
        if action == 'skip_story':
            # The skip button is mashy by nature: 2-4 human-paced presses.
            for _ in range(rng.randint(1, 3)):
                await asyncio.sleep(rng_uniform(rng, .15, .45))
                await call('click', dev_of(win), x=x0+px, y=y0+py, humanize=True)
            event(window=win['name'], skip_mash=True)
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
        if all(next_target(s) is None for s in states):
            summary['status'] = 'complete'
        else:
            async with open_session(spec, args.repo.resolve(), {'RUSTDESK_SCREENSHOT_DIR': ''}) as session:
                call = make_call(session, stopping)
                await locate_targets(cfg, call, args.output, 'fuben')
                keep = lease_keeper(call, all_devs)
                worker = asyncio.create_task(member_worker()) if other_members else None
                if cfg.get('autocal', True):
                    await autocalibrate()  # windows are re-located before any input
                summary['status'] = 'running'
                while not stopping():
                    started = time.monotonic()
                    await keep(started)
                    for i, win in enumerate(cfg['windows']):
                        s = states[i]
                        if s.get('paused') or next_target(s) is None:
                            continue
                        try:
                            image = await capture(win)
                            image.save(args.output/f"latest-{win['name']}.png")
                            obs, items = await asyncio.to_thread(recognize, image, wf, anchors[i], s['active'], cache=caches[i])
                            if obs['state'] == 'battle' and obs['source'] != 'local_battle_anchors':
                                anchors[i].learn(image)
                            elif obs['state'] not in ('unknown', 'battle'):
                                anchors[i] = BattleAnchors()
                            action = advance(s, obs, items, time.monotonic(), wf, cfg, args.execute, rng)
                            s['observation'] = obs
                            confirmed = s.pop('confirmed', None)
                            if confirmed:
                                after = shot_name(win['name'], confirmed+'-after')
                                image.save(args.output/after)
                                event(window=win['name'], confirmed=confirmed, after=after,
                                      knight=s['knight'], normal=s['normal'])
                            recovered = s.pop('recovered', None)
                            if recovered:
                                event(window=win['name'], recovered=recovered,
                                      knight=s['knight'], normal=s['normal'])
                            if s.get('fresh'):
                                event(window=win['name'], observation=obs,
                                      knight=s['knight'], normal=s['normal'])
                            persist()
                            if args.execute and action:
                                await execute_node(action, win, s, obs, items, caches[i])
                            save(args.output/f"recognition-{win['name']}.json", dict(observation=obs, items=items))
                            with (args.output/'timeline.jsonl').open('a') as f:
                                f.write(json.dumps(dict(time=time.time(), window=win['name'], **obs,
                                        knight=s['knight'], normal=s['normal'], action=action,
                                        pending=s.get('pending'), paused=s.get('paused'),
                                        warning=s.get('warning')), ensure_ascii=False)+'\n')
                        except Exception as exc:
                            if stopping():
                                break
                            s['paused'] = str(exc)
                            event(window=win['name'], error=str(exc), pending=s.get('pending'))
                        persist()
                    summary['loop_seconds'] = round(time.monotonic()-started, 3)
                    # A leader on the ready screen means members need readying:
                    # same-device members sweep inline (shared cursor),
                    # other devices run in member_worker().
                    waiting['now'] = any(
                        not s.get('paused') and next_target(s) is not None
                        and s.get('observation', {}).get('state') == 'knight_wait'
                        for s in states)
                    shared_swept = 0
                    if waiting['now']:
                        for dev in shared_members:
                            if stopping():
                                break
                            if time.monotonic() < member_next.get(dev, 0):
                                continue
                            try:
                                await sweep_members(dev)
                            except Exception as exc:
                                event(device=dev, member_error=str(exc))
                            cool_member(dev)
                            shared_swept += 1
                        persist()
                    budget = max(10, 7*len(states)) + (8*len(shared_members) if shared_swept else 0)
                    if summary['loop_seconds'] > budget:
                        raise RuntimeError('Loop exceeded observation budget; inspect latency')
                    if all(s.get('paused') or next_target(s) is None for s in states):
                        summary['status'] = ('complete' if all(next_target(s) is None for s in states)
                                             else 'paused')
                        break
                    await asyncio.sleep(max(.1, 1-(time.monotonic()-started)))
                if summary['status'] == 'running':
                    summary['status'] = 'stopped'
    except (Exception, BaseExceptionGroup) as exc:
        summary.update(status='stopped' if stopping() else 'paused', error=str(exc))
        event(error=str(exc))
    finally:
        if worker is not None:
            worker.cancel()
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
    inspect.add_argument('--rect', help='x,y,w,h content rectangle; reference images need the title bar removed')
    live = sub.add_parser('run')
    live.add_argument('--repo', type=Path, required=True)
    live.add_argument('--config', type=Path, required=True)
    live.add_argument('--output', type=Path, required=True)
    live.add_argument('--execute', action='store_true')
    live.add_argument('--knight', type=int, default=-1,
                      help='override knight runs per window; -1 keeps config/weekday default')
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
        obs, items = recognize(image, FubenWorkflow(build_screens(), []))
        print(json.dumps(dict(observation=obs, items=items), ensure_ascii=False, indent=2))
        return 0
    if args.knight >= 0:
        cfg = json.loads(args.config.read_text())
        cfg['knight_runs'] = args.knight
        args.config.write_text(json.dumps(cfg, ensure_ascii=False))
    if args.minutes < 0 or not math.isfinite(args.minutes):
        parser.error('invalid minutes')
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
