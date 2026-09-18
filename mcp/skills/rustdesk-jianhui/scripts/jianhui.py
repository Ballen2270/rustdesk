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
from local_features import BattleAnchors, green_label

SLOTS = [[.20, .65, .16, .15], [.37, .68, .16, .15],
         [.56, .65, .16, .15], [.75, .62, .17, .16]]


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


def classify(items, keyword='', slots=SLOTS):
    def within(it, box):
        x, y, w, h = box
        return x <= it['x']+it['w']/2 <= x+w and y <= it['y']+it['h']/2 <= y+h
    def has(text, box):
        return any(text in it['text'] and within(it, box) and it['score'] >= .25 for it in items)
    top = (0, 0, 1, .30)
    bottom = (.78, .89, .22, .11)
    panel = has('每周任务', (.25, .05, .45, .2)) and has('战斗历程', (.50, .15, .4, .15))
    result = has('胜负结果', (.3, .03, .4, .18)) and has('数据统计', (.3, .75, .4, .2))
    owner = has('房主', (0, .5, 1, .4)) or (bool(keyword) and has(keyword, (0, .5, 1, .4)))
    room = has('房间', top) and (not keyword or has(keyword, top)) and owner
    ready = [any(it['text'] == '准备' and it['score'] >= .35 and within(it, slot)
                 for it in items) for slot in slots]
    button = next((it for it in items if '天梯匹配' in it['text'] and within(it, bottom)), None)
    round_number = any(it['text'].isdigit() and within(it, (.42, 0, .16, .12)) for it in items)
    if result:
        state = 'result'
    elif panel:
        state = 'panel'
    elif room and has('取消匹配', bottom):
        state = 'matching'
    elif room and button:
        state = 'ready' if all(ready) else 'room'
    elif (sum(has(t, (.55, .5, .45, .5)) for t in ('法术', '攻击', '道具', '防御', '自动')) >= 3
          or ((has('攻击', (.78, .80, .15, .2)) or round_number)
              and has('取消', (.90, .80, .10, .2)))):
        state = 'battle'
    else:
        state = 'unknown'
    return dict(state=state, ready=ready, button=button)


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


def recognize(img, keyword='', slots=SLOTS, anchors=None, active=False):
    started = time.monotonic()
    items = region_ocr(img, (0, 0, 1, .30))
    items.extend(region_ocr(img, (0, .75, 1, .25)))
    source = 'roi_ocr'
    if any('房间' in it['text'] and it['y'] < .1 for it in items):
        items.extend(region_ocr(img, (0, .62, .98, .22), 2))
    obs = classify(items, keyword, slots)
    if obs['state'] in ('room', 'matching'):
        for ready, slot in zip(obs['ready'], slots):
            if not ready:
                items.extend(region_ocr(img, slot, 3))
        obs = classify(items, keyword, slots)
    for it in items:
        if (.62 <= it['y'] < .84 and it['score'] >= .25
                and re.sub(r'[^\u4e00-\u9fff]', '', it['text']) == '准备'
                and green_label(img, it)):
            it.update(raw_text=it['text'], raw_score=it['score'], text='准备', score=max(.5, it['score']), evidence='ocr+green_background')
    obs = classify(items, keyword, slots)
    if obs['state'] == 'unknown':
        items.extend(region_ocr(img, (.42, 0, .16, .12), 3))
        obs = classify(items, keyword, slots)
    if obs['state'] == 'unknown':
        items.extend(ocr(img))
        obs = classify(items, keyword, slots)
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
        s.update(seen=state, streak=1, since=now)
        return None
    s['streak'] += 1
    if state == 'unknown' and now-s['since'] > 20:
        s['warning'] = 'unknown screen; inputs withheld while local observation continues'
    timeout = {'room': room_timeout, 'matching': 600, 'result': 90}.get(state)
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
        elif now-s['sent'] > 12:
            s['paused'] = 'action not confirmed: '+pending
        if s.get('pending') or s.get('paused'):
            return None
    if state == 'battle':
        s['active'] = True
    elif state == 'result' and s.get('active'):
        s['count'] += 1
        s['active'] = False
    elif state in ('panel', 'room', 'ready'):
        if s.get('active'):
            s['paused'] = 'returned without observed result; reconcile count'
        elif state == 'panel':
            return 'close'
        elif state == 'ready':
            return 'match'
    return None


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def shot_name(win, tag):
    return f"{time.time_ns()}-{win}-{tag}.png"


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


def validate(cfg, games):
    if cfg.get('device') != 'pc1' or not cfg.get('calibrated'):
        raise ValueError('Requires calibrated pc1 configuration')
    nw, nh = cfg['native_resolution']
    windows = cfg['windows']
    if len(windows) != 2 or len(set(w['name'] for w in windows)) != 2:
        raise ValueError('Exactly two distinct windows required')
    for win in windows:
        x, y, w, h = win['rect']
        if not re.fullmatch(r'[a-z0-9_-]+', win['name']) or not win['room_keyword']:
            raise ValueError('Invalid name or missing room identity')
        if min(x, y) < 0 or not 300 <= w <= 2560 or h < 300 or x+w > nw or y+h > nh:
            raise ValueError('Invalid native content rectangle')
    a, b = [win['rect'] for win in windows]
    if max(a[0], b[0]) < min(a[0]+a[2], b[0]+b[2]) and max(a[1], b[1]) < min(a[1]+a[3], b[1]+b[3]):
        raise ValueError('Game content rectangles must not overlap')
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
                async def call(name, **kwargs):
                    if stopping():
                        raise InterruptedError('stop requested')
                    result = await asyncio.wait_for(session.call_tool(name, {'device': 'pc1', **kwargs}), 12)
                    if result.is_error:
                        raise RuntimeError(str(result.content))
                    return result.content
                async def capture(win):
                    x, y, w, h = win['rect']
                    content = await call('screenshot', format='png', max_width=2560,
                                         crop_x=x, crop_y=y, crop_w=w, crop_h=h)
                    note = ' '.join(c.text for c in content if c.type == 'text')
                    dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
                    if not dims or list(map(int, dims.groups())) != cfg['native_resolution']:
                        raise RuntimeError('Resolution changed; recalibration required')
                    blob = next(c.data for c in content if c.type == 'image')
                    img = Image.open(io.BytesIO(base64.b64decode(blob))).convert('RGB')
                    if img.size != (w, h):
                        raise RuntimeError('Crop resized or clipped')
                    return img
                lease = 0
                loop_index = 0
                anchors = [BattleAnchors() for _ in cfg['windows']]
                summary['status'] = 'running'
                while not stopping():
                    loop_index += 1
                    started = time.monotonic()
                    if started >= lease:
                        await call('keep_alive', minutes=5)
                        lease = started+60
                    for i, win in enumerate(cfg['windows']):
                        s = states[i]
                        if s.get('paused') or s['count'] >= args.games:
                            continue
                        img = await capture(win)
                        img.save(args.output/f"latest-{win['name']}.png")
                        obs, items = await asyncio.to_thread(recognize, img, win['room_keyword'], win.get('ready_slots', cfg.get('ready_slots', SLOTS)), anchors[i], s.get('active', False))
                        if not s.get('initialized'):
                            if obs['state'] not in ('room', 'ready', 'panel'):
                                s['paused'] = 'start requires prepared room; reconcile ongoing match'
                            s['initialized'] = True
                        prior = s.get('seen')
                        had_pending = s.get('pending')
                        action = advance(s, obs, time.monotonic(), args.games, cfg.get('room_timeout', 90))
                        confirmed = had_pending if had_pending and not s.get('pending') else None
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
                        if args.keep_frames == 'all':
                            frame = f"frames/{loop_index:06d}-{win['name']}.jpg"
                            img.save(args.output/frame, quality=75)
                            entry['frame'] = frame
                        timeline(entry)
                        if action and args.execute:
                            # Recheck the target immediately before any input, after other window work.
                            check_img = await capture(win)
                            check, _ = await asyncio.to_thread(recognize, check_img, win['room_keyword'], win.get('ready_slots', cfg.get('ready_slots', SLOTS)))
                            if check['state'] != obs['state']:
                                event(window=win['name'], action_aborted=action, reason='state changed on recheck')
                            else:
                                if time.monotonic()-started > 10:
                                    raise RuntimeError('Observation loop too slow for bounded input')
                                before = shot_name(win['name'], action+'-before')
                                check_img.save(args.output/before)
                                x, y, w, h = win['rect']
                                if action == 'match':
                                    b = check['button']
                                    px, py = b['x']+b['w']/2, b['y']+b['h']/2
                                else:
                                    px, py = cfg.get('panel_close', [.907, .122])
                                s.update(pending=action, sent=time.monotonic())
                                persist()
                                event(window=win['name'], intent=action, before=before)
                                await call('click', x=round(x+w*px), y=round(y+h*py), humanize=False)
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
                      help="all additionally saves every loop frame as frames/*.jpg (~1GB/hour)")
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
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
