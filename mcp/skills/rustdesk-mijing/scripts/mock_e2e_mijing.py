#!/usr/bin/env python3
"""Offline end-to-end for mijing: mock MCP -> runner -> artifacts -> viewer.

Drives jianhui-free mijing.py run against _framework/mock.py through a real
MCP stdio session. The scenario walks two windows through the full 25-level
round (panel navigation, per-level info-panel clicks, fight menus at 18/24/25,
leave) and asserts the run completes with the full artifact set, only
resolved clicks, humanized non-repeating coordinates, and a live viewer API.
Exit code 0 = pass.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SKILL = REPO/'mcp/skills/rustdesk-mijing/scripts'
FRAMEWORK = REPO/'mcp/skills/_framework'
PY = sys.executable
VIEWER_PORT = 8790
RUN_TIMEOUT = 480

CONFIG = dict(
    device='pc1', calibrated=True, native_resolution=[2400, 900],
    rounds=1, humanize=True, rng_seed=20260920,
    activity_button=[0.24, 0.0, 0.12, 0.11],
    activity_list_scroll=[0.45, 0.25, 0.35, 0.40],
    windows=[dict(name='left', rect=[0, 0, 1150, 880]),
             dict(name='right', rect=[1250, 0, 1150, 880])],
)

results = []


def check(ok, label, detail=''):
    results.append(ok)
    print(('  PASS  ' if ok else '  FAIL  ')+label+(f' — {detail}' if detail and not ok else ''),
          flush=True)


def main():
    tmp = Path(tempfile.mkdtemp(prefix='mijing-e2e-'))
    config_path = tmp/'config.json'
    config_path.write_text(json.dumps(CONFIG, ensure_ascii=False))
    clicks_path = tmp/'clicks.jsonl'
    spec = dict(mcpServers=dict(rustdesk=dict(
        command=PY,
        args=[str(FRAMEWORK/'mock.py')],
        env=dict(RUSTDESK_MOCK_SCENARIO=str(SKILL/'mijing_scenario.json'),
                 RUSTDESK_MOCK_CLICKS=str(clicks_path)))))
    spec_path = tmp/'spec.json'
    spec_path.write_text(json.dumps(spec))
    run_dir = tmp/'run'

    print('=== mijing mock e2e: run --execute --rounds 1 ===', flush=True)
    proc = subprocess.run(
        [PY, str(SKILL/'mijing.py'), 'run', '--repo', str(REPO), '--config', str(config_path),
         '--output', str(run_dir), '--execute', '--rounds', '1', '--mcp-spec', str(spec_path)],
        capture_output=True, text=True, timeout=RUN_TIMEOUT)
    check(proc.returncode == 0, 'run exit code 0 (complete)',
          f'rc={proc.returncode} stderr={proc.stderr[-800:]}')

    def read_jsonl(name):
        path = run_dir/name
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines()]

    state = json.loads((run_dir/'state.json').read_text()) if (run_dir/'state.json').exists() else {}
    check(state.get('status') == 'complete', 'state.json status complete', str(state.get('status')))
    check(state.get('mode') == 'execute', 'mode execute')
    windows = state.get('windows', [])
    check(len(windows) == 2 and all(w.get('count') == 1 for w in windows),
          'both windows counted 1 round', json.dumps(windows, default=str)[:300])

    timeline = read_jsonl('timeline.jsonl')
    check(len(timeline) > 40, 'timeline has per-loop rows', str(len(timeline)))
    battle_loops = [e for e in timeline if e.get('state') == 'battle']
    check(len(battle_loops) >= 10, 'battle states observed', str(len(battle_loops)))
    levels = {e.get('level') for e in timeline if e.get('window') == 'left'}
    check(1 in levels or 18 in levels or 24 in levels or 25 in levels,
          'level numbers parsed from OCR', str(sorted(l for l in levels if l is not None))[:120])
    check(all(e.get('loop_seconds', 0) <= 15 for e in timeline
              if 'loop_seconds' in e), 'timeline rows logged')

    events = read_jsonl('events.jsonl')
    intents = [e for e in events if 'intent' in e]
    sent = [e for e in events if 'sent' in e]
    check(len(intents) >= 8 and len(intents) == len(sent), 'every intent has a sent',
          f'{len(intents)} vs {len(sent)}')
    check(any(e.get('intent') == 'enter_fight' for e in intents), 'fight menu entered')
    check(any(e.get('intent') == 'leave' for e in intents), 'cleared leave clicked')
    confirmed = {e.get('confirmed') for e in events if 'confirmed' in e}
    check('leave' in confirmed, 'leave confirmed by normal screen', str(confirmed))

    clicks = [json.loads(l) for l in clicks_path.read_text().splitlines()] if clicks_path.exists() else []
    scrolls = [c for c in clicks if c.get('action') == 'scroll']
    check(len(clicks) >= 16, 'mock recorded clicks', str(len(clicks)))
    unresolved = [c for c in clicks if c.get('action') == 'unresolved']
    check(not unresolved, 'all clicks resolved against the scenario',
          json.dumps(unresolved[:3]))
    check(not scrolls, 'no scroll needed with the entry visible', str(len(scrolls)))
    for win in ('left', 'right'):
        pts = [(c['x'], c['y']) for c in clicks if c.get('window') == win]
        check(len(pts) == len(set(pts)), f'no repeated click coordinates ({win})',
              f'{len(pts)} clicks, {len(set(pts))} unique')

    for name in ('latest-left.png', 'latest-right.png',
                 'recognition-left.json', 'recognition-right.json'):
        check((run_dir/name).exists(), f'artifact {name}')
    before_shots = list(run_dir.glob('*-before.png'))
    after_shots = list(run_dir.glob('*-after.png'))
    check(len(before_shots) >= 8 and len(after_shots) >= 8,
          'before/after evidence shots', f'{len(before_shots)}/{len(after_shots)}')

    print('=== viewer on the finished run ===', flush=True)
    viewer = subprocess.Popen(
        [PY, str(SKILL.parent.parent/'rustdesk-jianhui'/'scripts'/'viewer.py'),
         '--run-dir', str(run_dir), '--port', str(VIEWER_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        with urllib.request.urlopen(f'http://127.0.0.1:{VIEWER_PORT}/', timeout=5) as r:
            check(r.status == 200, 'viewer serves html')
        with urllib.request.urlopen(f'http://127.0.0.1:{VIEWER_PORT}/api/data', timeout=5) as r:
            data = json.loads(r.read())
            check(data.get('stopped') is False, 'viewer data: not stopped')
            check((data.get('state') or {}).get('status') == 'complete',
                  'viewer data shows complete')
    finally:
        viewer.terminate()

    print(f'{sum(results)}/{len(results)} checks', flush=True)
    print(f'artifacts kept at {tmp}')
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
