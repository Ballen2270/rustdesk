#!/usr/bin/env python3
"""Bounded two-window Jianhui runner. No live connection in inspect/self-test."""
from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from PIL import Image

sys.path.append(str(Path(__file__).resolve().parents[2]))  # mcp/skills, for _framework
from _framework.artifacts import save, shot_name
from _framework.vision import BattleAnchors, green_label, ocr, region_ocr
from _framework.window_targets import locate_targets

SLOTS = [[.20, .65, .16, .15], [.37, .68, .16, .15],
         [.56, .65, .16, .15], [.75, .62, .17, .16]]

# Recognition regions as fractions of the game-content rectangle. Defaults match
# the original full-screen 1920x1080 window layout; a config "bands" mapping
# (top-level or per window) overrides individual keys for other layouts, e.g.
# two half-screen clients side by side on one display.
DEFAULT_BANDS = {
    'top': (0, 0, 1, .30),            # room title strip
    'bottom': (.78, .89, .22, .11),   # 天梯匹配 / 取消匹配 button
    'room_band': (0, .62, .98, .22),  # extra OCR strip once a room title is seen
    'ready_y': (.62, .84),            # y range where raw 准备 marks may be normalized
    'owner': (0, .5, 1, .4),          # 房主 / keyword confirmation strip
    'panel_title': (.25, .05, .45, .2), 'panel_body': (.50, .15, .4, .15),
    'result_title': (.3, .03, .4, .18), 'result_body': (.3, .75, .4, .2),
    'kick_button': (.50, .52, .30, .18), 'kick_wait': (.24, .52, .26, .18),
    'round': (.42, 0, .16, .12),      # battle round counter at the top
    'battle_moves': (.55, .5, .45, .5), 'battle_attack': (.78, .80, .15, .2),
    'battle_cancel': (.90, .80, .10, .2),
    'modal': (.20, .30, .60, .42),    # kick-warning modal scan band
}


def merge_bands(overrides=None):
    bands = dict(DEFAULT_BANDS)
    if overrides:
        unknown = set(overrides) - set(bands)
        if unknown:
            raise ValueError(f'Unknown band keys: {sorted(unknown)}')
        bands.update({k: tuple(v) for k, v in overrides.items()})
    return bands


def classify(items, keyword='', slots=SLOTS, bands=None):
    bands = bands or DEFAULT_BANDS
    def within(it, box):
        x, y, w, h = box
        return x <= it['x']+it['w']/2 <= x+w and y <= it['y']+it['h']/2 <= y+h
    def has(text, box):
        return any(text in it['text'] and within(it, box) and it['score'] >= .25 for it in items)
    top = bands['top']
    bottom = bands['bottom']
    panel = has('每周任务', bands['panel_title']) and has('战斗历程', bands['panel_body'])
    result = has('胜负结果', bands['result_title']) and has('数据统计', bands['result_body'])
    owner = has('房主', bands['owner']) or (bool(keyword) and has(keyword, bands['owner']))
    room = has('房间', top) and (not keyword or has(keyword, top)) and owner
    ready = [any(it['text'] == '准备' and it['score'] >= .35 and within(it, slot)
                 for it in items) for slot in slots]
    button = next((it for it in items if '天梯匹配' in it['text'] and within(it, bottom)), None)
    kick_button = next((it for it in items if '开始匹配' in it['text'] and within(it, bands['kick_button'])), None)
    kickwarn = kick_button is not None and has('我再等等', bands['kick_wait'])
    round_number = any(it['text'].isdigit() and within(it, bands['round']) for it in items)
    if result:
        state = 'result'
    elif panel:
        state = 'panel'
    elif kickwarn:
        state = 'kickwarn'
    elif room and has('取消匹配', bottom):
        state = 'matching'
    elif room and button:
        state = 'ready' if all(ready) else 'room'
    elif (sum(has(t, bands['battle_moves']) for t in ('法术', '攻击', '道具', '防御', '自动')) >= 3
          or ((has('攻击', bands['battle_attack']) or round_number)
              and has('取消', bands['battle_cancel']))):
        state = 'battle'
    else:
        state = 'unknown'
    return dict(state=state, ready=ready, button=kick_button if state == 'kickwarn' else button)


def recognize(img, keyword='', slots=SLOTS, anchors=None, active=False, bands=None):
    bands = bands or DEFAULT_BANDS
    started = time.monotonic()
    items = region_ocr(img, bands['top'])
    items.extend(region_ocr(img, (0, .75, 1, .25)))
    source = 'roi_ocr'
    if any('房间' in it['text'] and it['y'] < .1 for it in items):
        items.extend(region_ocr(img, bands['room_band'], 2))
    obs = classify(items, keyword, slots, bands)
    if obs['state'] in ('room', 'matching'):
        for ready, slot in zip(obs['ready'], slots):
            if not ready:
                items.extend(region_ocr(img, slot, 3))
        obs = classify(items, keyword, slots, bands)
    ready_lo, ready_hi = bands['ready_y']
    for it in items:
        if (ready_lo <= it['y'] < ready_hi and it['score'] >= .25
                and re.sub(r'[^\u4e00-\u9fff]', '', it['text']) == '准备'
                and green_label(img, it)):
            it.update(raw_text=it['text'], raw_score=it['score'], text='准备', score=max(.5, it['score']), evidence='ocr+green_background')
    obs = classify(items, keyword, slots, bands)
    if obs['state'] in ('room', 'ready'):
        # The kick-warning modal sits mid-screen; its band is only scanned there.
        items.extend(region_ocr(img, bands['modal'], 2))
        obs = classify(items, keyword, slots, bands)
    if obs['state'] == 'unknown':
        items.extend(region_ocr(img, bands['round'], 3))
        obs = classify(items, keyword, slots, bands)
    if obs['state'] == 'unknown':
        items.extend(ocr(img))
        obs = classify(items, keyword, slots, bands)
        source = 'full_ocr_fallback'
    # Learned anchors can only maintain an already-confirmed battle, never start one.
    if obs['state'] == 'unknown' and active and anchors is not None and anchors.matches(img):
        obs['state'] = 'battle'
        source = 'local_battle_anchors'
    obs.update(source=source, recognition_seconds=round(time.monotonic()-started, 4))
    return obs, items


def advance(s, obs, now, target, room_timeout=90):
    """Two consecutive observations before transitions; never retry an input."""
    state = obs['state']
    if s.get('paused') or s['count'] >= target:
        return None
    if state != 'unknown':
        s.pop('warning', None)
    if s.get('seen') != state:
        if state == 'result' and s.get('active'):
            # Result screens can auto-close within a single poll; count on first sight.
            s.update(seen=state, streak=2, since=now)
        else:
            s.update(seen=state, streak=1, since=now)
            return None
    else:
        s['streak'] += 1
    if state == 'unknown' and now-s['since'] > 20:
        s['warning'] = 'unknown screen; inputs withheld while local observation continues'
    timeout = {'room': room_timeout, 'matching': 600, 'result': 90, 'kickwarn': 45}.get(state)
    if timeout is not None and now-s['since'] > timeout:
        s['paused'] = 'state timeout: '+state
    if s.get('paused'):
        return None
    pending = s.get('pending')
    if pending:
        if pending == 'match' and state in ('matching', 'battle'):
            s.pop('pending', None)
        elif pending == 'close' and state in ('room', 'ready'):
            s.pop('pending', None)
        elif pending == 'rematch' and state in ('matching', 'battle', 'room', 'ready', 'panel'):
            s.pop('pending', None)
        elif now-s['sent'] > 20:
            s['paused'] = 'action not confirmed: '+pending
        if s.get('pending') or s.get('paused'):
            return None
    if state == 'battle':
        s['active'] = True
    elif state == 'result' and s.get('active'):
        s['count'] += 1
        s['active'] = False
    elif state == 'kickwarn':
        if s.get('active'):
            s['paused'] = 'kick warning during uncounted battle; reconcile'
        else:
            return 'rematch'
    elif state in ('panel', 'room', 'ready'):
        if s.get('active'):
            s['paused'] = 'returned without observed result; reconcile count'
        elif state == 'panel':
            return 'close'
        elif state == 'ready':
            return 'match'
    return None


def checkpoint(args):
    """Package a verified, stopped run into the documented --resume-state format."""
    state = json.loads(args.state.read_text())
    if state.get('status') == 'running':
        raise ValueError('Run still active; stop it before checkpointing')
    windows = []
    for s in state.get('windows', []):
        if s.get('pending') or s.get('paused'):
            raise ValueError('Reconcile pending or paused windows before checkpointing')
        windows.append(dict(count=s.get('count', 0), active=bool(s.get('active')),
                            initialized=bool(s.get('initialized'))))
    data = dict(config=state['config'], games=state['games'], windows=windows)
    save(args.out, data)
    return data


def configured_devices(cfg):
    """Return only the MCP devices referenced by configured windows."""
    return sorted({win.get('device', cfg['device']) for win in cfg['windows']})


def close_retry_needed(state, observed_state, now):
    """A close retry is safe to consider only while the panel is still visible."""
    return (state.get('pending') == 'close' and observed_state == 'panel'
            and not state.get('close_esc') and now-state.get('sent', 0) > 4)


def validate(cfg, games):
    if cfg.get('device') != 'pc1' or not cfg.get('calibrated'):
        raise ValueError('Requires calibrated pc1 configuration')
    base_resolution = cfg.get('native_resolution')
    if (not isinstance(base_resolution, list) or len(base_resolution) != 2
            or any(type(v) is not int or v < 1 for v in base_resolution)):
        raise ValueError('Invalid native_resolution')
    windows = cfg['windows']
    if len(windows) != 2 or len(set(w['name'] for w in windows)) != 2:
        raise ValueError('Exactly two distinct windows required')
    rectangles = []
    for win in windows:
        rect = win.get('rect')
        if (not isinstance(rect, list) or len(rect) != 4
                or any(type(v) is not int for v in rect)):
            raise ValueError('Invalid native content rectangle')
        x, y, w, h = rect
        if not re.fullmatch(r'[a-z0-9_-]+', win['name']) or not win['room_keyword']:
            raise ValueError('Invalid name or missing room identity')
        dev = win.get('device')
        if dev is not None and not re.fullmatch(r'[a-z0-9_-]+', dev):
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
        rectangles.append((win.get('device', cfg['device']), rect))
    (device_a, a), (device_b, b) = rectangles
    if device_a == device_b and max(a[0], b[0]) < min(a[0]+a[2], b[0]+b[2]) and max(a[1], b[1]) < min(a[1]+a[3], b[1]+b[3]):
        raise ValueError('Game content rectangles must not overlap on the same device')
    counts = cfg.get('initial_counts', [0, 0])
    if len(counts) != 2 or any(type(n) is not int or not 0 <= n <= games for n in counts):
        raise ValueError('Invalid initial counts')
    room_timeout = cfg.get('room_timeout', 90)
    if type(room_timeout) is not int or room_timeout < 10:
        raise ValueError('Invalid room timeout (integer seconds, minimum 10)')
    slots = cfg.get('ready_slots', SLOTS)
    if len(slots) != 4 or any(len(r) != 4 or min(r) < 0 or r[2] <= 0 or r[3] <= 0 or r[0]+r[2] > 1 or r[1]+r[3] > 1 for r in slots):
        raise ValueError('Invalid ready slots')
    close = cfg.get('panel_close', [.907, .122])
    if len(close) != 2 or not all(0 < v < 1 for v in close):
        raise ValueError('Invalid panel close point')
    for scope, bands in (('config', cfg.get('bands')), *(('window '+w['name'], w.get('bands')) for w in windows)):
        if bands is None:
            continue
        if not isinstance(bands, dict):
            raise ValueError(f'Invalid bands in {scope}')
        unknown = set(bands) - set(DEFAULT_BANDS)
        if unknown:
            raise ValueError(f'Unknown band keys in {scope}: {sorted(unknown)}')
        for key, value in bands.items():
            if key == 'ready_y':
                ok = (isinstance(value, list) and len(value) == 2
                      and all(type(v) in (int, float) and 0 <= v <= 1 for v in value))
            else:
                ok = (isinstance(value, list) and len(value) == 4
                      and all(type(v) in (int, float) and 0 <= v <= 1 for v in value)
                      and value[2] > 0 and value[3] > 0)
            if not ok:
                raise ValueError(f'Invalid band {key} in {scope}')


async def run(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    cfg = json.loads(args.config.read_text())
    validate(cfg, args.games)
    repo = args.repo.resolve()
    if args.mcp_spec:
        spec = json.loads(args.mcp_spec.read_text())['mcpServers']['rustdesk']
    else:
        spec = json.loads((repo/'.mcp.json').read_text())['mcpServers']['rustdesk']
        if not spec.get('env', {}).get('RUSTDESK_DEVICES_CONFIG'):
            raise ValueError('Fleet MCP configuration required')
    lock = open('/private/tmp/rustdesk-jianhui-pc1.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    args.output.mkdir(parents=True, exist_ok=False)
    if args.keep_frames == 'all':
        (args.output/'frames').mkdir()
        print('keep-frames=all: saving every loop frame as JPEG (~1GB/hour for two windows)', flush=True)
    viewer_proc = None
    if args.viewer:
        viewer_proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent/'viewer.py'),
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
    states = [dict(count=n, active=False) for n in cfg.get('initial_counts', [0, 0])]
    if args.resume_state:
        checkpoint = json.loads(args.resume_state.read_text())
        if checkpoint.get('config') != cfg or checkpoint.get('games') != args.games:
            raise ValueError('Resume checkpoint configuration mismatch')
        states = checkpoint['windows']
        if len(states) != 2 or any(s.get('pending') or s.get('paused') for s in states):
            raise ValueError('Reconcile paused or pending actions before explicit resume')
    summary = dict(mode='execute' if args.execute else 'observe', status='starting',
                   games=args.games, config=cfg, windows=states)
    def log_jsonl(name, entry):
        with (args.output/name).open('a') as f:
            f.write(json.dumps(entry, ensure_ascii=False)+'\n')
    def event(**entry):
        log_jsonl('events.jsonl', dict(time=time.time(), **entry))
    def timeline(entry):
        log_jsonl('timeline.jsonl', entry)
    def persist():
        save(args.output/'state.json', summary)
    params = StdioServerParameters(command=spec['command'], args=spec['args'], cwd=str(repo),
                                  env={**os.environ, **spec.get('env', {}), 'RUSTDESK_SCREENSHOT_DIR': ''})
    persist()
    try:
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await asyncio.wait_for(session.initialize(), 30)
                async def call(name, device, **kwargs):
                    if stopping():
                        raise InterruptedError('stop requested')
                    result = await asyncio.wait_for(session.call_tool(name, {'device': device, **kwargs}), 12)
                    if result.is_error:
                        raise RuntimeError(str(result.content))
                    return result.content

                await locate_targets(cfg, call, args.output, 'jianhui')

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
                devices = configured_devices(cfg)
                last_frame: dict[str, float] = {}
                lease = 0
                loop_index = 0
                anchors = [BattleAnchors() for _ in cfg['windows']]
                win_slots = [w.get('ready_slots', cfg.get('ready_slots', SLOTS)) for w in cfg['windows']]
                win_bands = [merge_bands(w.get('bands', cfg.get('bands'))) for w in cfg['windows']]
                summary['status'] = 'running'
                while not stopping():
                    loop_index += 1
                    started = time.monotonic()
                    if started >= lease:
                        for dev in devices:
                            await call('keep_alive', dev, minutes=5)
                        lease = started+60
                    for i, win in enumerate(cfg['windows']):
                        s = states[i]
                        if s.get('paused') or s['count'] >= args.games:
                            continue
                        img = await capture(win)
                        img.save(args.output/f"latest-{win['name']}.png")
                        obs, items = await asyncio.to_thread(recognize, img, win['room_keyword'], win_slots[i], anchors[i], s.get('active', False), win_bands[i])
                        if not s.get('initialized'):
                            if obs['state'] not in ('room', 'ready', 'panel'):
                                s['paused'] = 'start requires prepared room; reconcile ongoing match'
                                await save_alignment_evidence(win_device(win), f'start state {obs["state"]}')
                            s['initialized'] = True
                        prior = s.get('seen')
                        had_pending = s.get('pending')
                        action = advance(s, obs, time.monotonic(), args.games, cfg.get('room_timeout', 90))
                        confirmed = had_pending if had_pending and not s.get('pending') else None
                        if confirmed:
                            s.pop('close_esc', None)
                        if args.execute and close_retry_needed(s, obs['state'], time.monotonic()):
                            # Re-observe immediately before the retry; the first right-click may
                            # already have closed the panel between regular polls.
                            retry_img = await capture(win)
                            retry_obs, _ = await asyncio.to_thread(
                                recognize, retry_img, win['room_keyword'],
                                win_slots[i], None, False, win_bands[i])
                            if retry_obs['state'] == 'panel':
                                s['close_esc'] = True
                                s['sent'] = time.monotonic()
                                before = shot_name(win['name'], 'close-retry-before')
                                retry_img.save(args.output/before)
                                x, y, w, h = win['rect']
                                px, py = cfg.get('panel_close', [.907, .122])
                                tx, ty = round(x+w*px), round(y+h*py)
                                event(window=win['name'], intent='close-retry', before=before)
                                await call('click', win_device(win), x=tx, y=ty,
                                           humanize=False, button='right')
                                event(window=win['name'], sent='close-retry')
                            else:
                                event(window=win['name'], action_aborted='close-retry',
                                      reason='state changed on recheck',
                                      observed=retry_obs['state'])
                        if s.get('active') and obs['state'] == 'battle' and obs['source'] != 'local_battle_anchors':
                            anchors[i].learn(img)
                        if obs['state'] in ('room', 'ready', 'result', 'panel'):
                            anchors[i] = BattleAnchors()
                        s['observation'] = obs
                        save(args.output/f"recognition-{win['name']}.json", dict(observation=obs, items=items, warning=s.get('warning')))
                        if prior != obs['state']:
                            evidence = shot_name(win['name'], obs['state'])
                            event(window=win['name'], observation=obs, count=s['count'], evidence=evidence)
                            img.save(args.output/evidence)
                        if confirmed:
                            after = shot_name(win['name'], confirmed+'-after')
                            img.save(args.output/after)
                            event(window=win['name'], confirmed=confirmed, after=after, count=s['count'])
                        entry = dict(time=time.time(), loop=loop_index, window=win['name'],
                                     state=obs['state'], source=obs['source'], ready=obs['ready'],
                                     count=s['count'], active=s.get('active', False),
                                     pending=s.get('pending'), action=action, confirmed=confirmed,
                                     warning=s.get('warning'), paused=s.get('paused'),
                                     recognition_seconds=obs['recognition_seconds'],
                                     anchors=len(anchors[i].references))
                        if args.keep_frames == 'all' and time.monotonic()-last_frame.get(win['name'], 0) >= args.frame_interval:
                            last_frame[win['name']] = time.monotonic()
                            frame = f"frames/{loop_index:06d}-{win['name']}.jpg"
                            img.save(args.output/frame, quality=75)
                            entry['frame'] = frame
                        timeline(entry)
                        if action and args.execute:
                            # Recheck the target immediately before any input, after other window work.
                            check_img = await capture(win)
                            check, _ = await asyncio.to_thread(recognize, check_img, win['room_keyword'], win_slots[i], None, False, win_bands[i])
                            if check['state'] != obs['state']:
                                event(window=win['name'], action_aborted=action, reason='state changed on recheck')
                            else:
                                if time.monotonic()-started > 10:
                                    raise RuntimeError('Observation loop too slow for bounded input')
                                before = shot_name(win['name'], action+'-before')
                                check_img.save(args.output/before)
                                x, y, w, h = win['rect']
                                if action in ('match', 'rematch'):
                                    b = check['button']
                                    px, py = b['x']+b['w']/2, b['y']+b['h']/2
                                else:
                                    px, py = cfg.get('panel_close', [.907, .122])
                                button = 'right' if action == 'close' else 'left'
                                s.update(pending=action, sent=time.monotonic())
                                persist()
                                event(window=win['name'], intent=action, before=before)
                                await call('click', win_device(win), x=round(x+w*px), y=round(y+h*py), humanize=False, button=button)
                                event(window=win['name'], sent=action)
                        persist()
                    summary['loop_seconds'] = round(time.monotonic()-started, 3)
                    if summary['loop_seconds'] > 10:
                        raise RuntimeError('Double-window loop exceeded 10 seconds')
                    persist()
                    if all(s['count'] >= args.games or s.get('paused') for s in states):
                        summary['status'] = 'complete' if all(s['count'] >= args.games for s in states) else 'paused'
                        break
                    await asyncio.sleep(max(.1, 1-(time.monotonic()-started)))
                if summary['status'] == 'running':
                    summary['status'] = 'stopped'
    except (Exception, BaseExceptionGroup) as exc:
        summary.update(status='stopped' if stopping() else 'paused', error=str(exc))
        event(error=str(exc))
    finally:
        persist()
        if viewer_proc and viewer_proc.poll() is None:
            viewer_proc.terminate()
        lock.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary['status'] == 'paused' else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--image', type=Path, required=True)
    inspect.add_argument('--rect', help='x,y,w,h game content rectangle')
    inspect.add_argument('--keyword', default='')
    live = sub.add_parser('run')
    live.add_argument('--repo', type=Path, required=True)
    live.add_argument('--config', type=Path, required=True)
    live.add_argument('--output', type=Path, required=True)
    live.add_argument('--execute', action='store_true')
    live.add_argument('--resume-state', type=Path, help='Explicitly verified checkpoint; never automatic recovery')
    live.add_argument('--games', type=int, default=10)
    live.add_argument('--minutes', type=float, default=0, help='Optional task limit in minutes; 0 means no time limit')
    live.add_argument('--mcp-spec', type=Path, help='Alternate mcpServers spec JSON (offline mock); skips the fleet config check')
    live.add_argument('--keep-frames', choices=('changes', 'all'), default='changes',
                      help="all additionally saves loop frames as frames/*.jpg, sampled every --frame-interval")
    live.add_argument('--frame-interval', type=float, default=5.0,
                      help='seconds between saved frames/ keyframes in keep-frames=all (0 = every loop)')
    live.add_argument('--viewer', action='store_true', help='Spawn the read-only web viewer on the run directory')
    live.add_argument('--viewer-port', type=int, default=8788)
    ckpt = sub.add_parser('checkpoint', help='Package a stopped, reconciled run into an explicit --resume-state file')
    ckpt.add_argument('--state', type=Path, required=True, help='state.json of the stopped run')
    ckpt.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'inspect':
        img = Image.open(args.image)
        if args.rect:
            x, y, w, h = map(int, args.rect.split(','))
            img = img.crop((x, y, x+w, y+h))
        started = time.monotonic()
        observation, items = recognize(img, args.keyword)
        print(json.dumps(dict(image=str(args.image), seconds=round(time.monotonic()-started, 3),
                              observation=observation, items=items), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'checkpoint':
        print(json.dumps(checkpoint(args), ensure_ascii=False, indent=2))
        return 0
    if not 1 <= args.games <= 10 or not math.isfinite(args.minutes) or args.minutes < 0:
        parser.error('games must be 1..10; minutes must be finite and >= 0 (0 disables the task limit)')
    if not math.isfinite(args.frame_interval) or args.frame_interval < 0:
        parser.error('frame-interval must be finite and >= 0')
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
