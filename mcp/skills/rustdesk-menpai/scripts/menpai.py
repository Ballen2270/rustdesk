#!/usr/bin/env python3
"""Bounded multi-window 门派闯关 (sect challenge) runner on _framework.workflow.

Team daily activity (9:00, ~40-50 min): claim the menpai task at 门派使者,
then the right-hand task panel drives the loop — click 挑战xx护法 → auto
navigate → 进入战斗 → auto battle → next protector, 14 per chain; when the
panel shows 领取门派闯关任务 the chain is complete and a new claim starts.
The activity ends when, after a battle, the panel offers neither a protector
nor a claim. Two clients on pc1 = two independent teams, driven serially.
Recognition is local OCR (Apple Vision) + battle anchors; every click/scroll
goes through the humanized planner. The model only calibrates, reports and
reconciles pauses.
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
from _framework.window_targets import locate_targets
from _framework.workflow import (ClickEntry, ClickPoint, ClickText, Node, Screen, Workflow,
                                 find_row_button, find_text)

# Normalized bands (relative to the window content rect, below the title
# bar). Fixed UI anchors live here; everything else is dynamic noise.
TOP_BAR = (0.0, 0.0, 1.0, 0.12)        # 活动/指引/排行 buttons, panel titles
LEFT_TABS = (0.0, 0.14, 0.30, 0.30)    # 日常活动 tab
PANEL_TITLE = (0.25, 0.06, 0.55, 0.16)  # 活动 panel title
LIST_AREA = (0.20, 0.17, 0.78, 0.52)   # activity entry rows
TASK_BOX = (0.70, 0.12, 0.30, 0.24)    # task panel top entry 门派闯关(N/14)
MENU_HEAD = (0.60, 0.36, 0.40, 0.32)   # 请选择要做的事 heading
MENU_OPTS = (0.60, 0.48, 0.40, 0.47)   # menu buttons (menus sit below 0.48,
                                       # clear of the task panel and chat)
ROUND_BOX = (0.42, 0.0, 0.16, 0.12)
CANCEL_BOX = (0.90, 0.80, 0.10, 0.20)
TOOLBAR_BOX = (0.55, 0.5, 0.45, 0.5)
ATTACK_BOX = (0.78, 0.80, 0.15, 0.2)

# Takeover is allowed from any recognized screen (the task panel and menus
# are self-describing mid-flow); only a screen that stays unrecognized for
# START_GRACE seconds refuses the takeover. Inputs stay gated on
# `initialized`, so nothing is sent while the first screens settle.
START_GRACE = 20


def _center_in(it, box):
    x, y, w, h = box
    return x <= it['x']+it['w']/2 <= x+w and y <= it['y']+it['h']/2 <= y+h


def _contains(items, text, box):
    return any(text in it['text'] and it['score'] >= .25 and _center_in(it, box) for it in items)


def battle_hits(items):
    """Battle signature (jianhui-proven combo): a toolbar with ≥3 of the
    five combat words, or a round digit top-center plus 取消 bottom-right.
    Menpai battles show an icon-only toolbar where OCR yields none of that —
    a confident 取消 alone in the bottom-right control slot is accepted
    (that corner is empty on map and menu screens; screens are classified
    before battle anyway, so a menu's own 取消 cannot reach this rule)."""
    if sum(1 for t in ('法术', '攻击', '道具', '防御', '自动') if _contains(items, t, TOOLBAR_BOX)) >= 3:
        return True
    round_digit = any(it['text'].isdigit() and _center_in(it, ROUND_BOX) for it in items)
    if (_contains(items, '攻击', ATTACK_BOX) or round_digit) and _contains(items, '取消', CANCEL_BOX):
        return True
    return any('取消' in it['text'] and it['score'] >= .6 and _center_in(it, CANCEL_BOX)
               for it in items)


def task_n_of(items):
    """Parse (n, total) from the 门派闯关(N/14) task title. Parentheses are
    artwork and often misread, so match the bare digits. Returns
    (None, None) when absent (e.g. the 领取任务 entry)."""
    for it in items:
        if it['score'] < .25 or '闯关' not in it['text']:
            continue
        m = re.search(r'(\d{1,2})\s*/\s*(\d{1,2})', it['text'])
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def build_screens():
    """Declaration order = classify priority. Menus first (they overlap the
    map), then the task-panel states, then the bare map. `normal` forbids
    every menpai signature so a misread menu can never look like an ended
    activity — it degrades to `unknown` (inputs withheld) instead."""
    return [
        Screen('protector_menu', any_of=[[('请选择要做的事', MENU_HEAD), ('进入战斗', MENU_OPTS)]]),
        Screen('claim_menu', any_of=[[('请选择要做的事', MENU_HEAD), ('领取任务', MENU_OPTS)],
                                     [('领取任务', MENU_OPTS), ('便捷组队', MENU_OPTS)]]),
        Screen('messenger_menu', any_of=[[('请选择要做的事', MENU_HEAD), ('门派闯关', MENU_OPTS)],
                                         [('门派乱斗', MENU_OPTS), ('门派闯关', MENU_OPTS)]]),
        # Task entry: title 门派闯关(N/14) + subtitle 挑战xx护法 / 领取门派闯关任务.
        # 闯关 is matched bare — OCR often clips 门派 from the stylized title.
        # The 取消 forbid keeps battle-fade transitions (panel readable while
        # the battle cancel button lingers) classified as battle — clicking a
        # task entry mid-transition is exactly what must never happen.
        Screen('task_claim', any_of=[[('闯关', TASK_BOX), ('领取', TASK_BOX)]],
               forbid=[('取消', CANCEL_BOX, .6)]),
        Screen('task_protector', any_of=[[('闯关', TASK_BOX), ('挑战', TASK_BOX)]],
               forbid=[('取消', CANCEL_BOX, .6)]),
        Screen('activity_panel', any_of=[[('活动', PANEL_TITLE), ('日常活动', LEFT_TABS)]]),
        # 战斗切入瞬间地图元素未褪尽时 活动 仍可读 — 强 取消 在场即否定 normal，
        # 防止把战斗帧误判成地图而去点活动按钮。
        Screen('normal', any_of=[[('活动', (0.20, 0.0, 0.20, 0.10)), ('排行', (0.30, 0.0, 0.20, 0.10))]],
               forbid=[('闯关', TASK_BOX), ('领取', TASK_BOX),
                       ('请选择要做的事', MENU_HEAD), ('进入战斗', MENU_OPTS),
                       ('取消', CANCEL_BOX, .6)]),
    ]


class MenpaiWorkflow(Workflow):
    """classify() with battle_hits as a special screen (its signature is
    element-count based and cannot be expressed as a Screen). Screens are
    checked first — they carry negative evidence (menu option pairs, task
    entries); battle is the fallback, never a prefix override."""

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
    observing an expected screen, never from blind retry. Navigation clicks
    (walk to the protector / back to the messenger) get long timeouts."""
    entry = cfg.get('activity_entry', '门派闯关')
    nodes = [
        Node('open_activity', 'normal', ClickPoint(box=tuple(cfg['activity_button'])),
             expect=['activity_panel'], timeout=20),
        Node('join_entry', 'activity_panel',
             ClickEntry(entry=entry, button='参加', search_box=LIST_AREA,
                        max_scrolls=cfg.get('max_scrolls', 4),
                        scroll_box=tuple(cfg.get('activity_list_scroll', (.45, .25, .35, .40)))),
             expect=['messenger_menu', 'claim_menu'], timeout=120),
        Node('select_menpai', 'messenger_menu', ClickText(text='门派闯关', box=MENU_OPTS),
             expect=['claim_menu'], timeout=30),
        Node('claim_task', 'claim_menu', ClickText(text='领取任务', box=MENU_OPTS),
             expect=['task_protector'], timeout=60),
        Node('goto_protector', 'task_protector', ClickText(text='挑战', box=TASK_BOX),
             expect=['protector_menu', 'battle'], timeout=150),
        Node('enter_fight', 'protector_menu', ClickText(text='进入战斗', box=MENU_OPTS),
             expect=['battle'], timeout=30),
        Node('reclaim_loop', 'task_claim', ClickText(text='领取', box=TASK_BOX),
             expect=['claim_menu', 'messenger_menu'], timeout=150),
    ]
    return MenpaiWorkflow(build_screens(), nodes)


def recognize(img, wf, anchors=None, active=False):
    """Band OCR cascade → classification. Zooms: task entry at 2x (small
    text, N/14 digits), activity rows at 2x, menu options at 2x, full-frame
    fallback, then anchors (maintain battle only)."""
    started = time.monotonic()
    items = region_ocr(img, TOP_BAR)
    items += region_ocr(img, MENU_HEAD)
    items += region_ocr(img, MENU_OPTS)
    items += region_ocr(img, TASK_BOX)
    source = 'bands'
    state = wf.classify(items)
    if state in ('normal', 'task_protector', 'task_claim'):
        items += region_ocr(img, TASK_BOX, 2)
        state = wf.classify(items)
    if state == 'activity_panel':
        items += region_ocr(img, LIST_AREA, 2)
        state = wf.classify(items)
    if state == 'unknown':
        items += region_ocr(img, MENU_OPTS, 2)
        state = wf.classify(items)
    if state == 'unknown':
        # Icon-only battle toolbars (small windows): zoom the 取消 slot so its
        # OCR confidence clears the battle_hits threshold.
        items += region_ocr(img, CANCEL_BOX, 2)
        state = wf.classify(items)
    if state == 'unknown':
        items += ocr(img)
        state = wf.classify(items)
        source = 'full_ocr_fallback'
    # Learned anchors can only maintain an already-confirmed battle.
    if state == 'unknown' and active and anchors is not None and anchors.matches(img):
        state = 'battle'
        source = 'local_battle_anchors'
    task_n, task_total = task_n_of(items)
    obs = dict(state=state, source=source, task_n=task_n, task_total=task_total,
               recognition_seconds=round(time.monotonic()-started, 4))
    return obs, items


def bookkeep(s, obs, now, cfg):
    """Per-observation state bookkeeping (mutates s; no I/O). Chain
    semantics: a chain = one claimed task (14 protectors). Seeing the
    领取任务 entry completes the previous chain; the activity is over when a
    battle is followed by a bare map with neither a protector nor a claim
    entry for `end_grace` seconds."""
    state = obs['state']
    if state == 'battle':
        s['active'] = True
        s['battle_seen'] = True
    elif state == 'task_protector':
        if obs.get('task_n') is not None:
            s['task_n'], s['task_total'] = obs['task_n'], obs['task_total']
        s['chain'] = True
    elif state == 'task_claim':
        # Completes the chain only when we observed its battles; a takeover
        # that joins at the claim screen proceeds without counting that chain.
        if s.pop('chain', False) and s.get('battle_seen'):
            s['count'] += 1  # the 14-protector chain just ended
    elif state == 'normal':
        if s.get('battle_seen'):
            if s.pop('chain', False):
                # Activity died right after the final battle of a chain.
                if (s.get('task_n') is not None and s.get('task_total') is not None
                        and s['task_n'] >= s['task_total']):
                    s['count'] += 1
            s.setdefault('done_since', now)
            if now-s['done_since'] >= cfg.get('end_grace', 30):
                s['ended'] = 'activity over: no next protector or claim entry after battle'
        else:
            s.pop('done_since', None)
    else:
        s.pop('done_since', None)


def validate(cfg, rounds):
    if not cfg.get('calibrated'):
        raise ValueError('Requires calibrated configuration')
    if not 1 <= rounds <= 50:
        raise ValueError('rounds must be 1..50 (resolved from --rounds or config)')
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
        dev = win['device'] if 'device' in win else cfg.get('device')
        if not isinstance(dev, str):
            raise ValueError('Invalid window device name')
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
    entry = cfg.get('activity_entry', '门派闯关')
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError('Invalid activity_entry')
    grace = cfg.get('end_grace', 30)
    if type(grace) is not int or grace < 10:
        raise ValueError('Invalid end_grace (integer seconds, minimum 10)')


def resolve_viewer():
    """The read-only viewer is still shared with the jianhui skill until the
    console moves into _framework (Phase D)."""
    here = Path(__file__).resolve()
    for candidate in (here.parent/'viewer.py',
                      here.parents[2]/'rustdesk-jianhui'/'scripts'/'viewer.py'):
        if candidate.exists():
            return candidate
    return None


class StaleFrame(RuntimeError):
    pass


def require_fresh_frame(note):
    if not re.search(r'\bstale=False\b', note) or not re.search(r'\bframe_seq=\d+\b', note):
        raise StaleFrame('No new decoded frame; current game state is unconfirmed')


def hold_for_frame(s, now):
    s.setdefault('frame_wait_since', now)
    s['seen'] = 'stale'
    s['observation'] = dict(state='stale', source='transport')
    s['warning'] = 'No new decoded frame; recognition and inputs withheld'
    s.pop('done_since', None)


def resume_after_frame(s, now):
    since = s.pop('frame_wait_since', None)
    if since is not None:
        for key in ('sent', 'init_since', 'unknown_since'):
            if key in s:
                s[key] += now-since
        s.pop('warning', None)


async def run(args):
    cfg = json.loads(args.config.read_text())
    rounds = args.rounds if args.rounds > 0 else cfg.get('rounds', 20)
    validate(cfg, rounds)
    wf = build_workflow(cfg)
    repo = args.repo.resolve()
    spec = load_spec(args.mcp_spec, repo)
    devices = sorted({w.get('device', cfg['device']) for w in cfg['windows']})
    locks = []
    for dev in devices:
        lock = open(f'/private/tmp/rustdesk-menpai-{dev}.lock', 'a')
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
    states = [dict(count=n, active=False, initialized=False, battle_seen=False,
                   chain=False, task_n=None, task_total=None, ended=None, fresh=True)
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
                             crop_x=x, crop_y=y, crop_w=w, crop_h=h,
                             wait_for_new_frame=True)
        note = ' '.join(c.text for c in content if c.type == 'text')
        require_fresh_frame(note)
        dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
        if not dims or list(map(int, dims.groups())) != res:
            raise RuntimeError('Resolution changed; recalibration required')
        blob = next(c.data for c in content if c.type == 'image')
        img = Image.open(io.BytesIO(base64.b64decode(blob))).convert('RGB')
        if img.size != (w, h):
            raise RuntimeError('Crop resized or clipped')
        return img

    async def capture_device(dev):
        """Full-device frame at native resolution — miscalibration evidence."""
        content = await call('screenshot', dev, format='png', max_width=2560)
        blob = next(c.data for c in content if c.type == 'image')
        return Image.open(io.BytesIO(base64.b64decode(blob))).convert('RGB')

    saved_evidence = set()

    async def save_alignment_evidence(dev, why):
        """One full-desktop screenshot per device per run: the picture the
        operator recalibrates window rects from. Best-effort only."""
        if dev in saved_evidence:
            return
        saved_evidence.add(dev)
        try:
            full = await capture_device(dev)
            name = f'misaligned-{dev}.png'
            full.save(args.output/name)
            event(device=dev, misaligned=name, reason=why)
        except Exception:  # noqa: BLE001 — evidence is best-effort
            pass

    anchor_fixed = {'done': False}

    async def humanized_scroll(win, box):
        anchor = (box[0]+box[2]/2, box[1]+box[3]/2)
        x0, y0, w, h = win['rect']
        if scroller is None:
            await capture(win)
            await call('scroll', win_device(win), x=x0+round(anchor[0]*w), y=y0+round(anchor[1]*h), dy=3)
            return
        for x, y, dy, pause in scroller.segment(anchor, 1, (w, h)):
            await capture(win)
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
            await locate_targets(cfg, call, args.output, 'menpai')
            keep = lease_keeper(call, devices)
            summary['status'] = 'running'
            loop_index = 0
            while not stopping():
                loop_index += 1
                started = time.monotonic()
                await keep(started)
                for i, win in enumerate(cfg['windows']):
                    s = states[i]
                    if s.get('paused') or s.get('ended') or s['count'] >= rounds:
                        continue
                    try:
                        img = await capture(win)
                    except StaleFrame:
                        if 'frame_wait_since' not in s:
                            event(window=win['name'], warning='No new decoded frame; inputs withheld')
                        hold_for_frame(s, time.monotonic())
                        continue
                    resume_after_frame(s, time.monotonic())
                    img.save(args.output/f"latest-{win['name']}.png")
                    obs, items = await asyncio.to_thread(
                        recognize, img, wf, anchors[i], s.get('active', False))
                    now = time.monotonic()
                    if not s.get('initialized'):
                        if obs['state'] != 'unknown':
                            s['initialized'] = True
                            s.pop('init_since', None)
                        else:
                            # 首帧落在加载/战斗切换等瞬态画面：给识别器 START_GRACE
                            # 秒收敛，仍未识别才判为坏接管（期间不发任何输入）。
                            s.setdefault('init_since', now)
                            if now-s['init_since'] > START_GRACE:
                                s['initialized'] = True
                                s['paused'] = ('start never reached a recognized screen — window '
                                               'rect may be stale; recalibrate from '
                                               f'misaligned-{win_device(win)}.png in the run dir')
                                await save_alignment_evidence(win_device(win), 'unknown start')

                    # ---- startup alignment check (every run, no asking): the
                    # configured rect must actually contain the game client —
                    # windows move between sessions. The 活动 anchor is
                    # re-derived from live OCR when it drifted; every other
                    # click targets an OCR item and is shift-invariant. ----
                    if (not s.get('anchored') and not s.get('paused')
                            and obs['state'] not in ('battle', 'unknown')):
                        upper = [it for it in items if it['score'] >= .25
                                 and it['y']+it['h']/2 <= .45]
                        if not any(t in it['text'] for it in upper
                                   for t in ('活动', '排行', '指引')):
                            s['paused'] = ('window rect misaligned: no game top-bar UI inside '
                                           'the configured rect; recalibrate from '
                                           f'misaligned-{win_device(win)}.png in the run dir')
                            await save_alignment_evidence(win_device(win), 'no top-bar UI')
                        else:
                            s['anchored'] = True
                            act = find_text(items, '活动', (0.0, 0.0, 0.45, 0.12))
                            if act is not None and not anchor_fixed['done']:
                                anchor_fixed['done'] = True
                                node = wf.nodes.get('open_activity')
                                if node is not None:
                                    old = tuple(node.act.box)
                                    node.act.box = (act['x'], act['y'], act['w'], act['h'])
                                    event(activity_button=dict(
                                        old=[round(v, 4) for v in old],
                                        new=[round(act[k], 4) for k in ('x', 'y', 'w', 'h')]),
                                        note='re-derived from OCR at startup')
                    prior = s.get('seen')
                    s['seen'] = obs['state']
                    s['fresh'] = prior != obs['state']
                    s['observation'] = dict(obs)

                    # ---- battle bookkeeping (anchors learn; chain/end tracking) ----
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
                            if confirmed == 'claim_task':
                                # a fresh chain starts: previous battles no longer count
                                s['battle_seen'] = False
                                s['task_n'] = None
                                s['task_total'] = None
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
                    candidates = wf.nodes_for(obs['state'])
                    if args.execute:
                        if (not s.get('paused') and not s.get('ended') and not s.get('pending')
                                and obs['state'] != 'unknown' and s['count'] < rounds
                                and s.get('initialized') and candidates):
                            try:
                                action = await execute_node(candidates[0], win, s, obs)
                            except StaleFrame:
                                hold_for_frame(s, time.monotonic())
                                event(window=win['name'], action_aborted=candidates[0].id,
                                      reason='No new decoded frame on input recheck')
                                continue
                    elif candidates:
                        action = candidates[0].id

                    if confirmed:
                        after = shot_name(win['name'], confirmed+'-after')
                        img.save(args.output/after)
                        event(window=win['name'], confirmed=confirmed, after=after,
                              count=s['count'], task_n=s.get('task_n'))
                    entry = dict(time=time.time(), loop=loop_index, window=win['name'],
                                 state=obs['state'], source=obs['source'],
                                 task_n=obs.get('task_n'), count=s['count'],
                                 active=s.get('active', False), battle_seen=s.get('battle_seen', False),
                                 pending=s.get('pending'), action=action, confirmed=confirmed,
                                 ended=s.get('ended'), warning=s.get('warning'), paused=s.get('paused'),
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
                if all(s['count'] >= rounds or s.get('paused') or s.get('ended') for s in states):
                    summary['status'] = 'complete' if all(
                        s['count'] >= rounds or s.get('ended') for s in states) else 'paused'
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
                      help='override config rounds; 1..50 (0 = use config rounds)')
    live.add_argument('--minutes', type=float, default=0,
                      help='optional task limit in minutes; 0 means no limit')
    live.add_argument('--mcp-spec', type=Path,
                      help='alternate mcpServers spec JSON (offline mock)')
    live.add_argument('--viewer', action='store_true')
    live.add_argument('--viewer-port', type=int, default=8791)
    args = parser.parse_args()
    if args.command == 'inspect':
        img = Image.open(args.image)
        if args.rect:
            x, y, w, h = map(int, args.rect.split(','))
            img = img.crop((x, y, x+w, y+h))
        started = time.monotonic()
        observation, items = recognize(img.convert('RGB'), MenpaiWorkflow(build_screens(), []))
        print(json.dumps(dict(image=str(args.image), seconds=round(time.monotonic()-started, 3),
                              observation=observation, items=items), ensure_ascii=False, indent=2))
        return 0
    if not 1 <= (args.rounds if args.rounds > 0 else 20) <= 50:
        parser.error('rounds must be 1..50 (0 = use config rounds)')
    if not math.isfinite(args.minutes) or args.minutes < 0:
        parser.error('minutes must be finite and >= 0')
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
