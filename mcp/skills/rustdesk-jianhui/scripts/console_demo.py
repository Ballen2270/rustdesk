#!/usr/bin/env python3
"""Console demo: fabricate a rich run directory, feed it live, serve the viewer.

Builds a mock mijing-style run (two windows, real reference screenshots as
latest frames and evidence shots, a full timeline/events history), then a
feeder thread keeps appending rows so the console shows a live session:
swimlanes grow, the log scrolls, counts increment, and the run completes.
The viewer itself stays read-only exactly as in production — only the
fabricated artifacts change. Ctrl-C exits; the demo dir lives under /tmp.

Usage: console_demo.py [--port 8799] [--no-feed]
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from viewer import Handler, Viewer

REPO = Path(__file__).resolve().parents[4]
IMG = REPO/'images'
DEMO = Path('/tmp/rustdesk-console-demo')
PORT = 8799

WIN = {'normal': 'normal/游戏正常界面.png', 'activity_panel': 'normal/活动面板.png',
       'yunleyou_menu': 'normal/云乐游-秘境降妖.png', 'mijing_panel': 'normal/秘境降妖面板.png',
       'in_instance': 'normal/进入秘境降妖.png', 'fight_menu': 'normal/秘境降妖18关.png',
       'battle': '战斗ing.png', 'cleared': 'normal/通关.png', 'unknown': 'normal/进入秘境降妖.png'}
ACTION_OF = {'activity_panel': 'open_activity', 'yunleyou_menu': 'join_entry',
             'mijing_panel': 'select_entry', 'in_instance': 'continue_challenge'}
NODE_SRC = {'open_activity': 'normal', 'join_entry': 'activity_panel',
            'select_entry': 'yunleyou_menu', 'continue_challenge': 'mijing_panel',
            'enter_fight': 'fight_menu', 'advance_level': 'in_instance', 'leave': 'cleared'}


def shot(run, win, tag, state):
    name = f"{time.time_ns()}-{win}-{tag}.png"
    shutil.copyfile(IMG/WIN[state], run/name)
    return name


def row(t, loop, win, state, level, count, **kw):
    base = dict(time=t, loop=loop, window=win, state=state,
                source=kw.pop('source', 'roi_ocr' if state != 'unknown' else 'full_ocr_fallback'),
                level=level, count=count, active=kw.pop('active', state == 'battle'),
                pending=kw.pop('pending', None), action=kw.pop('action', None),
                confirmed=kw.pop('confirmed', None), warning=kw.pop('warning', None),
                paused=kw.pop('paused', None),
                recognition_seconds=round(random.uniform(.18, .62), 4),
                anchors=kw.pop('anchors', None))
    base.update(kw)
    return base


def build_history(run, start, loop0):
    """Interleaved two-window history: left completes its round; right is
    mid-run at level 7 when the demo starts (the feeder continues it)."""
    rng = random.Random(7)
    left, right = [], []

    def seq(out, win, spec, count):
        for state, n, lv in spec:
            for i in range(n):
                pending = action = confirmed = None
                if state in ACTION_OF and i == 0:
                    action, pending = ACTION_OF[state], ACTION_OF[state]
                out.append((state, lv, count, action, pending))

    seq(left, 'left', [('normal', 3, None), ('activity_panel', 2, None),
                       ('yunleyou_menu', 2, None), ('mijing_panel', 2, None)], 0)
    for level in range(1, 18):
        seq(left, 'left', [('in_instance', 3, level), ('battle', 4, level)], 0)
    seq(left, 'left', [('fight_menu', 2, 18), ('battle', 4, None),
                       ('fight_menu', 2, 24), ('battle', 4, None),
                       ('fight_menu', 2, 25), ('battle', 4, None),
                       ('cleared', 3, None), ('normal', 4, None)], 0)
    seq(right, 'right', [('normal', 3, None), ('activity_panel', 2, None),
                         ('yunleyou_menu', 2, None), ('mijing_panel', 2, None)], 0)
    for level in range(1, 7):
        seq(right, 'right', [('in_instance', 3, level), ('battle', 4, level)], 0)
    seq(right, 'right', [('in_instance', 2, 7), ('unknown', 3, 7),
                         ('in_instance', 2, 7), ('battle', 3, 7)], 0)

    rows, ev = [], []
    t, loop = start, loop0
    for _ in range(max(len(left), len(right))):
        for win, seq_ in (('left', left), ('right', right)):
            if not seq_:
                continue
            state, level, count, action, pending = seq_.pop(0)
            r = row(t, loop, win, state, level, count if win == 'left' and state == 'normal' and not seq_ or True else count,
                    action=action, pending=pending if state in ACTION_OF else None,
                    anchors=6 if state == 'battle' else None,
                    warning='unknown screen; inputs withheld while local observation continues' if state == 'unknown' else None)
            if state == 'unknown':
                r['active'] = False
            rows.append(r)
            ev.append(dict(time=t, window=win, observation=dict(
                state=state, source=r['source'], level=level,
                recognition_seconds=r['recognition_seconds']), count=count,
                evidence=shot(run, win, state, state)))
            if action:
                before = shot(run, win, action+'-before', NODE_SRC[action])
                ev.append(dict(time=t+.01, window=win, intent=action, before=before, fresh=True))
                ev.append(dict(time=t+.6, window=win, sent=action))
        t += 2.0
        loop += 1
    # left's leave confirmation closes its round
    ev.append(dict(time=t, window='left', confirmed='leave',
                   after=shot(run, 'left', 'leave-after', 'normal'), count=1, level=None))
    return rows, ev, t, loop


def write_seed(run):
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True)
    start = time.time()-720
    rows, ev, t, loop = build_history(run, start, 1)
    with (run/'timeline.jsonl').open('w') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False)+'\n')
    with (run/'events.jsonl').open('w') as f:
        for e in ev:
            f.write(json.dumps(e, ensure_ascii=False)+'\n')
    for win, state in (('left', 'normal'), ('right', 'battle')):
        shutil.copyfile(IMG/WIN[state], run/f'latest-{win}.png')
        (run/f'recognition-{win}.json').write_text(json.dumps(dict(
            observation=dict(state=state, source='roi_ocr',
                             level=7 if win == 'right' else None,
                             recognition_seconds=.31),
            items=[dict(text=t_, score=.9, x=.8, y=.4, w=.1, h=.04)
                   for t_ in ('秘境降妖', '第7关：挑战蟹将', '地图', '离开')],
            warning=None), ensure_ascii=False))
    state = dict(
        mode='execute', status='running', games=1, loop_seconds=1.9,
        config=dict(device='pc1', calibrated=True, native_resolution=[2400, 900],
                    rounds=1,
                    windows=[dict(name='left', rect=[0, 0, 1150, 880]),
                             dict(name='right', rect=[1250, 0, 1150, 880])]),
        windows=[dict(count=1, active=False, initialized=True, level=None,
                      seen='normal', inst_since=None, inst_level=None,
                      battle_seen=False, fresh=False, pending=None,
                      observation=dict(state='normal', source='roi_ocr')),
                 dict(count=0, active=True, initialized=True, level=7,
                      seen='battle', inst_since=None, inst_level=7,
                      battle_seen=True, fresh=False, pending=None,
                      observation=dict(state='battle', source='roi_ocr'))])
    (run/'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return state


def feed(run, state):
    """Advance the right window through levels 7..25, then complete."""
    tl = (run/'timeline.jsonl').open('a')
    evj = (run/'events.jsonl').open('a')
    wins = state['windows']
    t0, loop = time.time(), 500
    spec = []
    for level in range(7, 18):
        spec += [('in_instance', 3, level), ('battle', 4, level)]
    spec += [('fight_menu', 2, 18), ('battle', 4, None), ('fight_menu', 2, 24),
             ('battle', 4, None), ('fight_menu', 2, 25), ('battle', 4, None),
             ('cleared', 3, None), ('normal', 4, None)]
    for state_, n, level in spec:
        for i in range(n):
            t = time.time()
            action = ACTION_OF.get(state_) if i == 0 else None
            r = row(t, loop, 'right', state_, level, 0, action=action,
                    pending=action, anchors=6 if state_ == 'battle' else None)
            tl.write(json.dumps(r, ensure_ascii=False)+'\n')
            tl.flush()
            if state_ != 'unknown':
                evj.write(json.dumps(dict(time=t, window='right',
                    observation=dict(state=state_, source=r['source'], level=level,
                                     recognition_seconds=r['recognition_seconds']),
                    count=0, evidence=shot(run, 'right', state_, state_)),
                    ensure_ascii=False)+'\n')
            if action:
                evj.write(json.dumps(dict(time=t+.01, window='right', intent=action,
                    before=shot(run, 'right', action+'-before', NODE_SRC[action]),
                    fresh=True), ensure_ascii=False)+'\n')
                evj.write(json.dumps(dict(time=t+.6, window='right', sent=action),
                                     ensure_ascii=False)+'\n')
            evj.flush()
            wins[1].update(seen=state_, active=state_ == 'battle', level=level,
                           inst_level=level, pending=None,
                           observation=dict(state=state_, source=r['source']))
            if state_ == 'battle':
                wins[1]['battle_seen'] = True
            state['loop_seconds'] = round(random.uniform(1.4, 2.6), 2)
            tmp = run/'state.json.tmp'
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            tmp.replace(run/'state.json')
            shutil.copyfile(IMG/WIN[state_], run/'latest-right.png')
            loop += 1
            time.sleep(1.6)
    wins[1].update(count=1, active=False, battle_seen=False, level=None, seen='normal')
    state['status'] = 'complete'
    tmp = run/'state.json.tmp'
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(run/'state.json')
    evj.write(json.dumps(dict(time=time.time(), window='right', confirmed='leave',
        after=shot(run, 'right', 'leave-after', 'normal'), count=1), ensure_ascii=False)+'\n')
    tl.close()
    evj.close()
    print('demo: right window completed its round — status complete', flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--no-feed', action='store_true', help='static history only')
    args = ap.parse_args()
    state = write_seed(DEMO)
    if not args.no_feed:
        threading.Thread(target=feed, args=(DEMO, state), daemon=True).start()
    viewer = Viewer(DEMO)
    Handler.viewer = viewer
    httpd = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'console demo: http://127.0.0.1:{args.port}  (run dir {DEMO})', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\ndemo stopped', flush=True)


if __name__ == '__main__':
    main()
