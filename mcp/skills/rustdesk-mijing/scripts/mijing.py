#!/usr/bin/env python3
"""Bounded multi-window Mijing (秘境降妖) runner built on _framework.workflow.

Single-player daily activity: each window is one account walking the flow
normal → activity panel → join → (云乐游 menu) → mijing panel → continue →
in-instance loop (auto battles; later levels pop a fight menu) → cleared →
leave. Recognition is local OCR (Apple Vision) + battle anchors; every
click/scroll goes through the humanized planner. The model only calibrates,
reports and reconciles pauses.
"""
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
import subprocess
import sys
import time

from PIL import Image

sys.path.append(str(Path(__file__).resolve().parents[2]))  # mcp/skills, for _framework
from _framework.artifacts import event_logger, save, shot_name
from _framework.humanize import ClickPlanner, ScrollPlanner
from _framework.session import load_spec, lease_keeper, make_call, open_session
from _framework.vision import BattleAnchors, ocr, region_ocr
from _framework.workflow import (ClickEntry, ClickPoint, ClickText, Node, Screen, Workflow,
                                 find_row_button, find_text, level_of)

# Normalized bands (relative to the window content rect). Fixed UI anchors
# live here; everything else on screen is treated as dynamic noise.
TOP_BAR = (0.0, 0.0, 1.0, 0.17)        # 活动/指引/排行 buttons, panel titles
LEFT_TABS = (0.0, 0.14, 0.30, 0.30)    # 日常活动 tab
PANEL_TITLE = (0.25, 0.06, 0.55, 0.16)  # 活动 / 秘境降妖 panel titles
RIGHT_MID = (0.60, 0.28, 0.40, 0.32)   # instance info panel, menu titles
RIGHT_LOW = (0.60, 0.55, 0.40, 0.36)   # menu buttons, 继续挑战, 取消
LIST_AREA = (0.20, 0.17, 0.78, 0.52)   # activity entry rows
LEVEL_LINE = (0.66, 0.34, 0.34, 0.15)  # 第N关 line (zoom target)
MENU_MIJING = (0.66, 0.40, 0.34, 0.16)  # 云乐游 menu 秘境降妖 option
ROUND_BOX = (0.42, 0.0, 0.16, 0.12)
CANCEL_BOX = (0.90, 0.80, 0.10, 0.20)
TOOLBAR_BOX = (0.55, 0.5, 0.45, 0.5)
ATTACK_BOX = (0.78, 0.80, 0.15, 0.2)


def _center_in(it, box):
    x, y, w, h = box
    return x <= it['x']+it['w']/2 <= x+w and y <= it['y']+it['h']/2 <= y+h


def _contains(items, text, box):
    return any(text in it['text'] and it['score'] >= .25 and _center_in(it, box) for it in items)


def battle_hits(items):
    """Battle signature (jianhui-proven combo): a toolbar with ≥3 of the
    five combat words, or a round digit top-center plus 取消 bottom-right.
    Auto battle often shows only 取消 and skill icons — do not require the
    full toolbar."""
    if sum(1 for t in ('法术', '攻击', '道具', '防御', '自动') if _contains(items, t, TOOLBAR_BOX)) >= 3:
        return True
    round_digit = any(it['text'].isdigit() and _center_in(it, ROUND_BOX) for it in items)
    return (_contains(items, '攻击', ATTACK_BOX) or round_digit) and _contains(items, '取消', CANCEL_BOX)


def build_screens():
    """Declaration order = classify priority: specific screens (modals,
    menus) before generic ones; battle handled by MijingWorkflow."""
    return [
        Screen('cleared', any_of=[[('秘境降妖', RIGHT_MID), ('通关', LEVEL_LINE)]]),
        Screen('fight_menu', any_of=[[('请选择要做的事', RIGHT_MID), ('进入战斗', RIGHT_LOW)]]),
        Screen('yunleyou_menu', any_of=[[('请选择要做的事', RIGHT_MID), ('秘境降妖', MENU_MIJING)]]),
        # 继续挑战 is a vertical two-line button: OCR yields separate 继续 /
        # 挑战 items, and the artistic panel title misreads (•税境降妖), so
        # the signature anchors on the button pair instead of the title.
        Screen('mijing_panel', any_of=[[('继续', RIGHT_LOW), ('挑战', RIGHT_LOW)]]),
        Screen('in_instance',
               any_of=[[('秘境降妖', RIGHT_MID), ('地图', RIGHT_MID)],
                       [('秘境降妖', RIGHT_MID), ('离开', RIGHT_MID)],
                       [('地图', RIGHT_MID), ('离开', RIGHT_MID)]],
               forbid=[('继续', RIGHT_LOW)]),
        Screen('activity_panel', any_of=[[('活动', PANEL_TITLE), ('日常活动', LEFT_TABS)]]),
        Screen('normal', any_of=[[('活动', (0.20, 0.0, 0.20, 0.10)), ('排行', (0.30, 0.0, 0.20, 0.10))]]),
    ]


class MijingWorkflow(Workflow):
    """classify() with battle_hits as a special screen (its signature is
    element-count based and cannot be expressed as a Screen)."""

    def classify(self, items):
        for screen in self.screens:
            if screen.name == 'battle':
                continue
            if screen.match(items):
                return screen.name
        if battle_hits(items):
            return 'battle'
        return 'unknown'


def build_workflow(cfg):
    """The workflow graph. Each node = one step; confirmation comes from
    observing an expected screen, never from blind retry."""
    nodes = [
        Node('open_activity', 'normal', ClickPoint(box=tuple(cfg['activity_button'])),
             expect=['activity_panel'], timeout=20),
        Node('join_entry', 'activity_panel',
             ClickEntry(entry='秘境降妖', button='参加', search_box=LIST_AREA,
                        max_scrolls=cfg.get('max_scrolls', 4),
                        scroll_box=tuple(cfg.get('activity_list_scroll', (.45, .25, .35, .40)))),
             expect=['yunleyou_menu'], timeout=90),
        Node('select_entry', 'yunleyou_menu', ClickText(text='秘境降妖', box=MENU_MIJING),
             expect=['mijing_panel'], timeout=30),
        Node('continue_challenge', 'mijing_panel', ClickText(text='继续', box=RIGHT_LOW),
             expect=['in_instance', 'fight_menu', 'battle'], timeout=60),
        Node('enter_fight', 'fight_menu', ClickText(text='进入战斗', box=RIGHT_LOW),
             expect=['battle'], timeout=30),
        Node('advance_level', 'in_instance', ClickText(text='第', box=LEVEL_LINE),
             expect=['battle', 'fight_menu'], timeout=90,
             guard=lambda ctx: (ctx['s'].get('inst_since') is not None
                                and ctx['now']-ctx['s']['inst_since'] >= ctx['advance_delay'])),
        Node('leave', 'cleared', ClickText(text='离开', box=RIGHT_MID),
             expect=['normal'], timeout=30),
    ]
    return MijingWorkflow(build_screens(), nodes)


def recognize(img, wf, anchors=None, active=False):
    """Band OCR cascade → classification. Zooms: activity rows at 2x, level
    line at 3x, full-frame fallback, then anchors (maintain battle only)."""
    started = time.monotonic()
    items = region_ocr(img, TOP_BAR)
    items += region_ocr(img, LEFT_TABS)
    items += region_ocr(img, RIGHT_MID)
    items += region_ocr(img, RIGHT_LOW)
    source = 'bands'
    state = wf.classify(items)
    if state == 'activity_panel':
        items += region_ocr(img, LIST_AREA, 2)
        state = wf.classify(items)
    if state == 'in_instance' and level_of(items) is None:
        items += region_ocr(img, LEVEL_LINE, 3)
        state = wf.classify(items)
    if state == 'unknown':
        items += region_ocr(img, RIGHT_LOW, 2)  # small button text (继续/挑战)
        state = wf.classify(items)
    if state == 'unknown':
        items += ocr(img)
        state = wf.classify(items)
        source = 'full_ocr_fallback'
    # Learned anchors can only maintain an already-confirmed battle.
    if state == 'unknown' and active and anchors is not None and anchors.matches(img):
        state = 'battle'
        source = 'local_battle_anchors'
    obs = dict(state=state, source=source, level=level_of(items),
               recognition_seconds=round(time.monotonic()-started, 4))
    return obs, items


def bookkeep(s, obs, now, cfg):
    """Per-observation state bookkeeping (mutates s; no I/O). Carries the
    pause-don't-guess anomalies: a battle that returns to the same level did
    not advance (failed run → reconcile), and a cleared screen without any
    observed battle is not a countable round. fight_menu keeps the pending
    pre-battle level (boss chains fight through menus)."""
    state = obs['state']
    level = obs.get('level')
    if state == 'battle':
        if s.get('seen') == 'in_instance' and s.get('inst_level') is not None:
            s['pre_battle_level'] = s['inst_level']
        s['active'] = True
        s['battle_seen'] = True
        s['inst_since'] = None
    elif state == 'cleared':
        if not s.get('battle_seen'):
            s['paused'] = 'cleared without an observed battle; reconcile'
        s['inst_since'] = None
    elif state == 'in_instance':
        if s.get('pre_battle_level') is not None and level is not None:
            if level <= s['pre_battle_level']:
                s['paused'] = f'level did not advance after battle ({level}); reconcile'
            s['pre_battle_level'] = None  # level None defers the decision one poll
        if s.get('inst_since') is None or level != s.get('inst_level'):
            s.update(inst_since=now, inst_level=level)
        if (s.get('active') and s.get('battle_seen') and level is not None
                and level == s.get('inst_level')
                and now-s['inst_since'] > cfg.get('advance_stall', 150)):
            s['paused'] = f'no progress after battle (level {level}); reconcile'
    else:
        s['inst_since'] = None
        if state != 'fight_menu':
            s['pre_battle_level'] = None


def validate(cfg, rounds):
    if not cfg.get('calibrated'):
        raise ValueError('Requires calibrated configuration')
    if not 1 <= rounds <= 10:
        raise ValueError('rounds must be 1..10 (resolved from --rounds or config)')
    base_resolution = cfg.get('native_resolution')
    if (not isinstance(base_resolution, list) or len(base_resolution) != 2
            or any(type(v) is not int or v < 1 for v in base_resolution)):
        raise ValueError('Invalid native_resolution')
    windows = cfg['windows']
    if not 1 <= len(windows) <= 8 or len(set(w['name'] for w in windows)) != len(windows):
        raise ValueError('Requires 1..8 windows with distinct names')
    rectangles = []
    for win in windows:
        rect = win.get('rect')
        if (not isinstance(rect, list) or len(rect) != 4
                or any(type(v) is not int for v in rect)):
            raise ValueError('Invalid native content rectangle')
        x, y, w, h = rect
        if not re.fullmatch(r'[a-z0-9_-]+', win['name']):
            raise ValueError('Invalid window name')
        dev = win.get('device', cfg['device'])
        if not re.fullmatch(r'[a-z0-9_-]+', dev):
            raise ValueError('Invalid window device name')
        resolution = base_resolution
        if 'native_resolution' in win:
            r = win.get('native_resolution')
            if (not isinstance(r, list) or len(r) != 2
                    or any(type(v) is not int or v < 1 for v in r)):
                raise ValueError('Invalid window native_resolution')
            resolution = r
        nw, nh = resolution
        if min(x, y) < 0 or not 300 <= w <= 2560 or h < 300 or x+w > nw or y+h > nh:
            raise ValueError('Invalid native content rectangle')
        rectangles.append((dev, rect))
    for i, (dev_a, a) in enumerate(rectangles):
        for dev_b, b in rectangles[i+1:]:
            if dev_a == dev_b and max(a[0], b[0]) < min(a[0]+a[2], b[0]+b[2]) \
                    and max(a[1], b[1]) < min(a[1]+a[3], b[1]+b[3]):
                raise ValueError('Game content rectangles must not overlap on the same device')
    counts = cfg.get('initial_counts', [0]*len(windows))
    if len(counts) != len(windows) or any(type(n) is not int or not 0 <= n <= rounds for n in counts):
        raise ValueError('Invalid initial counts')
    for key in ('activity_button', 'activity_list_scroll'):
        box = cfg.get(key)
        if (not isinstance(box, list) or len(box) != 4
                or any(not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in box)
                or box[2] <= 0 or box[3] <= 0
                or box[0]+box[2] > 1 or box[1]+box[3] > 1):
            raise ValueError('Invalid '+key)
    if type(cfg.get('advance_delay', 5)) is not int or cfg.get('advance_delay', 5) < 0:
        raise ValueError('Invalid advance_delay')
    if type(cfg.get('advance_stall', 150)) is not int or cfg.get('advance_stall', 150) < 30:
        raise ValueError('Invalid advance_stall (integer seconds, minimum 30)')


def resolve_viewer():
    """The read-only viewer is still shared with the jianhui skill until the
    console moves into _framework (Phase D)."""
    here = Path(__file__).resolve()
    for candidate in (here.parent/'viewer.py',
                      here.parents[2]/'rustdesk-jianhui'/'scripts'/'viewer.py'):
        if candidate.exists():
            return candidate
    return None


async def run(args):
    cfg = json.loads(args.config.read_text())
    rounds = args.rounds if args.rounds > 0 else cfg.get('rounds', 1)
    validate(cfg, rounds)
    wf = build_workflow(cfg)
    repo = args.repo.resolve()
    spec = load_spec(args.mcp_spec, repo)
    devices = sorted({w.get('device', cfg['device']) for w in cfg['windows']})
    locks = []
    for dev in devices:
        lock = open(f'/private/tmp/rustdesk-mijing-{dev}.lock', 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    args.output.mkdir(parents=True, exist_ok=False)
    viewer_proc = None
    viewer_py = resolve_viewer()
    if args.viewer and viewer_py is not None:
        viewer_proc = subprocess.Popen(
            [sys.executable, str(viewer_py),
             '--run-dir', str(args.output), '--port', str(args.viewer_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(.5)
        if viewer_proc.poll() is None:
            print(f'viewer: http://127.0.0.1:{args.viewer_port}', flush=True)
        else:
            print(f'viewer exited immediately (rc={viewer_proc.returncode}); port busy?', flush=True)
    stopped = False

    def request_stop(*_):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    deadline = time.monotonic()+args.minutes*60 if args.minutes else math.inf

    def stopping():
        return stopped or (args.output/'STOP').exists() or time.monotonic() >= deadline

    humanize = cfg.get('humanize', True)
    rng = random.Random(cfg['rng_seed']) if cfg.get('rng_seed') is not None else random.Random()
    planner = ClickPlanner(rng) if humanize else None
    scroller = ScrollPlanner(rng) if humanize else None
    states = [dict(count=n, active=False, initialized=False, level=None,
                   inst_since=None, inst_level=None, battle_seen=False, fresh=True)
              for n in cfg.get('initial_counts', [0]*len(cfg['windows']))]
    anchors = [BattleAnchors() for _ in cfg['windows']]
    summary = dict(mode='execute' if args.execute else 'observe', status='starting',
                   games=rounds, config=cfg, windows=states)
    event = event_logger(args.output)

    def log_jsonl(name, entry):
        with (args.output/name).open('a') as f:
            f.write(json.dumps(entry, ensure_ascii=False)+'\n')

    def persist():
        save(args.output/'state.json', summary)

    def win_device(win):
        return win.get('device', cfg['device'])

    async def capture(win):
        x, y, w, h = win['rect']
        res = win.get('native_resolution', cfg['native_resolution'])
        content = await call('screenshot', win_device(win), format='png', max_width=2560,
                             crop_x=x, crop_y=y, crop_w=w, crop_h=h)
        note = ' '.join(c.text for c in content if c.type == 'text')
        dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
        if not dims or list(map(int, dims.groups())) != res:
            raise RuntimeError('Resolution changed; recalibration required')
        blob = next(c.data for c in content if c.type == 'image')
        img = Image.open(io.BytesIO(base64.b64decode(blob))).convert('RGB')
        if img.size != (w, h):
            raise RuntimeError('Crop resized or clipped')
        return img

    async def humanized_scroll(win, box):
        anchor = (box[0]+box[2]/2, box[1]+box[3]/2)
        x0, y0, w, h = win['rect']
        if scroller is None:
            await call('scroll', win_device(win), x=x0+round(anchor[0]*w), y=y0+round(anchor[1]*h), dy=3)
            return
        for x, y, dy, pause in scroller.segment(anchor, 1, (w, h)):
            await call('scroll', win_device(win), x=x0+x, y=y0+y, dy=dy)
            await asyncio.sleep(min(pause, .5))

    async def execute_node(node, win, s, obs):
        """Safety ordering: the human-like hesitation happens BEFORE the
        pre-click recheck, so the recheck frame is as close to the input as
        possible — never sleep between the recheck and the click."""
        if planner is not None:
            await asyncio.sleep(min(planner.delay(fresh_screen=s.get('fresh', False)), 1.0))
        check_img = await capture(win)
        check, check_items = await asyncio.to_thread(recognize, check_img, wf)
        if check['state'] != obs['state']:
            event(window=win['name'], action_aborted=node.id,
                  reason='state changed on recheck', observed=check['state'])
            return None
        act = node.act
        if isinstance(act, ClickPoint):
            box, button = tuple(act.box), act.button
        elif isinstance(act, ClickText):
            item = find_text(check_items, act.text, act.box)
            if item is None:
                event(window=win['name'], action_aborted=node.id, reason='text not found on recheck')
                return None
            box, button = (item['x'], item['y'], item['w'], item['h']), act.button
        elif isinstance(act, ClickEntry):
            target, btn = find_row_button(check_items, act.entry, act.button, act.search_box)
            if btn is not None:
                box, button = (btn['x'], btn['y'], btn['w'], btn['h']), 'left'
            elif target is None:
                if s.get('entry_attempts', 0) >= act.max_scrolls:
                    s['paused'] = f'entry {act.entry!r} not found after {act.max_scrolls} scrolls'
                    return None
                s['entry_attempts'] = s.get('entry_attempts', 0)+1
                event(window=win['name'], scroll=act.entry, attempt=s['entry_attempts'])
                await humanized_scroll(win, act.scroll_box)
                return node.id+':scroll'
            else:
                s['paused'] = f'entry {act.entry!r} visible but no {act.button!r} button; reconcile'
                return None
        else:  # Nop / unknown: nothing to send
            return None
        x0, y0, w, h = win['rect']
        if planner is None:
            x, y = round((box[0]+box[2]/2)*w), round((box[1]+box[3]/2)*h)
        else:
            x, y = planner.point(f'{win["name"]}:{node.id}', box, (w, h))
        s.update(pending=node.id, sent=time.monotonic())
        before = shot_name(win['name'], node.id+'-before')
        check_img.save(args.output/before)
        event(window=win['name'], intent=node.id, before=before, fresh=s.get('fresh'))
        await call('click', win_device(win), x=x0+x, y=y0+y, humanize=humanize, button=button)
        event(window=win['name'], sent=node.id)
        return node.id

    loop_budget = max(10.0, 7.0*len(cfg['windows']))
    try:
        async with open_session(spec, repo, {'RUSTDESK_SCREENSHOT_DIR': ''}) as session:
            call = make_call(session, stopping)
            keep = lease_keeper(call, devices)
            summary['status'] = 'running'
            loop_index = 0
            while not stopping():
                loop_index += 1
                started = time.monotonic()
                await keep(started)
                for i, win in enumerate(cfg['windows']):
                    s = states[i]
                    if s.get('paused') or s['count'] >= rounds:
                        continue
                    img = await capture(win)
                    img.save(args.output/f"latest-{win['name']}.png")
                    obs, items = await asyncio.to_thread(
                        recognize, img, wf, anchors[i], s.get('active', False))
                    now = time.monotonic()
                    if not s.get('initialized'):
                        if obs['state'] != 'normal':
                            s['paused'] = 'start requires the normal game screen; reconcile'
                        s['initialized'] = True
                    prior = s.get('seen')
                    s['seen'] = obs['state']
                    s['fresh'] = prior != obs['state']
                    s['observation'] = dict(obs)
                    s['level'] = obs.get('level')

                    # ---- battle bookkeeping (anchors learn; stall/failure tracking) ----
                    if obs['state'] == 'battle' and obs['source'] != 'local_battle_anchors':
                        anchors[i].learn(img)
                    bookkeep(s, obs, now, cfg)

                    # ---- pending confirmation (never blind-retry an input) ----
                    confirmed = None
                    if s.get('pending'):
                        node = wf.nodes[s['pending']]
                        if obs['state'] in node.expect:
                            confirmed = s['pending']
                            s.pop('pending', None)
                            s.pop('sent', None)
                            s['entry_attempts'] = 0
                            if confirmed == 'leave' and obs['state'] == 'normal':
                                s['count'] += 1
                                s['active'] = False
                                s['battle_seen'] = False
                        elif now-s['sent'] > node.timeout:
                            s['paused'] = 'action not confirmed: '+s['pending']

                    # ---- unknown-screen policy: inputs withheld ----
                    if obs['state'] == 'unknown' and not s.get('paused'):
                        s.setdefault('unknown_since', now)
                        if now-s['unknown_since'] > 20:
                            s['warning'] = 'unknown screen; inputs withheld while observation continues'
                    else:
                        s.pop('unknown_since', None)
                        s.pop('warning', None)

                    # ---- pick and execute the workflow node ----
                    action = None
                    ctx = dict(s=s, obs=obs, now=now, advance_delay=cfg.get('advance_delay', 5))
                    candidates = wf.nodes_for(obs['state'], ctx)
                    if args.execute:
                        if (not s.get('paused') and not s.get('pending')
                                and obs['state'] != 'unknown' and s['count'] < rounds
                                and s.get('initialized') and candidates):
                            action = await execute_node(candidates[0], win, s, obs)
                    elif candidates:
                        action = candidates[0].id

                    if confirmed:
                        after = shot_name(win['name'], confirmed+'-after')
                        img.save(args.output/after)
                        event(window=win['name'], confirmed=confirmed, after=after,
                              count=s['count'], level=s.get('level'))
                    entry = dict(time=time.time(), loop=loop_index, window=win['name'],
                                 state=obs['state'], source=obs['source'], level=obs.get('level'),
                                 count=s['count'], active=s.get('active', False),
                                 pending=s.get('pending'), action=action, confirmed=confirmed,
                                 warning=s.get('warning'), paused=s.get('paused'),
                                 recognition_seconds=obs['recognition_seconds'],
                                 anchors=len(anchors[i].references))
                    if prior is not None and prior != obs['state']:
                        evidence = shot_name(win['name'], obs['state'])
                        entry['evidence'] = evidence
                        img.save(args.output/evidence)
                        event(window=win['name'], observation=obs, count=s['count'],
                              evidence=evidence)
                    log_jsonl('timeline.jsonl', entry)
                    save(args.output/f"recognition-{win['name']}.json",
                         dict(observation=obs, items=items, warning=s.get('warning')))
                summary['loop_seconds'] = round(time.monotonic()-started, 3)
                if summary['loop_seconds'] > loop_budget:
                    raise RuntimeError('Loop exceeded budget; check recognition cost')
                persist()
                if all(s['count'] >= rounds or s.get('paused') for s in states):
                    summary['status'] = 'complete' if all(s['count'] >= rounds for s in states) else 'paused'
                    break
                await asyncio.sleep(max(.1, 1.0-(time.monotonic()-started)))
            if summary['status'] == 'running':
                summary['status'] = 'stopped'
    except (Exception, BaseExceptionGroup) as exc:
        summary.update(status='stopped' if stopping() else 'paused', error=str(exc))
        event(error=str(exc))
    finally:
        persist()
        if viewer_proc and viewer_proc.poll() is None:
            viewer_proc.terminate()
        for lock in locks:
            lock.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary['status'] == 'paused' else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    insp = sub.add_parser('inspect')
    insp.add_argument('--image', type=Path, required=True)
    insp.add_argument('--rect', help='x,y,w,h game content rectangle')
    live = sub.add_parser('run')
    live.add_argument('--repo', type=Path, required=True)
    live.add_argument('--config', type=Path, required=True)
    live.add_argument('--output', type=Path, required=True)
    live.add_argument('--execute', action='store_true')
    live.add_argument('--rounds', type=int, default=0,
                      help='override config rounds; 1..10 (0 = use config rounds)')
    live.add_argument('--minutes', type=float, default=0,
                      help='optional task limit in minutes; 0 means no limit')
    live.add_argument('--mcp-spec', type=Path,
                      help='alternate mcpServers spec JSON (offline mock)')
    live.add_argument('--viewer', action='store_true')
    live.add_argument('--viewer-port', type=int, default=8789)
    args = parser.parse_args()
    if args.command == 'inspect':
        img = Image.open(args.image)
        if args.rect:
            x, y, w, h = map(int, args.rect.split(','))
            img = img.crop((x, y, x+w, y+h))
        started = time.monotonic()
        observation, items = recognize(img.convert('RGB'), MijingWorkflow(build_screens(), []))
        print(json.dumps(dict(image=str(args.image), seconds=round(time.monotonic()-started, 3),
                              observation=observation, items=items), ensure_ascii=False, indent=2))
        return 0
    if not 1 <= (args.rounds if args.rounds > 0 else 1) <= 10:
        parser.error('rounds must be 1..10 (0 = use config rounds)')
    if not math.isfinite(args.minutes) or args.minutes < 0:
        parser.error('minutes must be finite and >= 0')
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
