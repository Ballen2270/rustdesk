#!/usr/bin/env python3
"""Offline end-to-end: mock MCP -> runner -> artifacts -> viewer. No device.

Drives jianhui.py run against mock_mcp.py through a real MCP stdio session,
asserts the run completes one scripted game per window with the full artifact
set (timeline, before/after shots, events, clicks), then serves the finished
run directory through viewer.py and checks its API. Exit code 0 = pass.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SKILL = REPO/'mcp/skills/rustdesk-jianhui/scripts'
PY = sys.executable
VIEWER_PORT = 8791
RUN_TIMEOUT = 240

WINDOWS = [dict(name='left', rect=[0, 0, 1150, 880], room_keyword='房间'),
           dict(name='right', rect=[1250, 0, 1150, 880], room_keyword='房间')]


def check(results, ok, label, detail=''):
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — '+detail) if detail and not ok else ''}")


def main():
    base = Path(tempfile.mkdtemp(prefix='jianhui-e2e-'))
    out = base/'run'
    clicks = base/'clicks.jsonl'
    cfg = dict(device='pc1', calibrated=True, native_resolution=[2400, 900],
               initial_counts=[0, 0], windows=WINDOWS,
               ready_slots=[[.20, .65, .16, .15], [.37, .68, .16, .15],
                            [.56, .65, .16, .15], [.75, .62, .17, .16]],
               panel_close=[.907, .122])
    (base/'config.json').write_text(json.dumps(cfg, ensure_ascii=False))
    spec = dict(mcpServers=dict(rustdesk=dict(
        command=PY, args=[str(SKILL/'mock_mcp.py')],
        env=dict(JIANHUI_MOCK_IMAGES=str(REPO/'images'),
                 JIANHUI_MOCK_CLICKS=str(clicks),
                 JIANHUI_MOCK_NATIVE='2400x900',
                 JIANHUI_MOCK_RIGHT_OFFSET='2'))))
    (base/'spec.json').write_text(json.dumps(spec))
    print(f'e2e base: {base}\nlaunching runner (1 scripted game per window, ~60s)…')
    proc = subprocess.Popen(
        [PY, str(SKILL/'jianhui.py'), 'run', '--repo', str(REPO),
         '--config', str(base/'config.json'), '--output', str(out),
         '--mcp-spec', str(base/'spec.json'), '--execute', '--games', '1'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time()+RUN_TIMEOUT
    state = {}
    try:
        while time.time() < deadline:
            time.sleep(2)
            if (out/'state.json').exists():
                try:
                    state = json.loads((out/'state.json').read_text())
                    print(f"  status={state.get('status')} counts="
                          f"{[w.get('count') for w in state.get('windows', [])]}")
                    if state.get('status') in ('complete', 'paused', 'stopped'):
                        break
                except json.JSONDecodeError:
                    pass
            if proc.poll() is not None and not (out/'state.json').exists():
                break
        rc = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    print(f'runner exit={rc}')
    tail = proc.stdout.read()
    if tail.strip():
        print('runner output:\n'+tail)

    results: list[bool] = []
    state = json.loads((out/'state.json').read_text()) if (out/'state.json').exists() else {}
    check(results, state.get('status') == 'complete', 'run completed', str(state.get('status')))
    counts = [w.get('count') for w in state.get('windows', [])]
    check(results, counts == [1, 1], 'both windows counted exactly one game', str(counts))
    check(results, rc == 0, 'runner exit code 0', str(rc))

    timeline = [json.loads(l) for l in (out/'timeline.jsonl').read_text().splitlines()] \
        if (out/'timeline.jsonl').exists() else []
    events = [json.loads(l) for l in (out/'events.jsonl').read_text().splitlines()] \
        if (out/'events.jsonl').exists() else []
    check(results, len(timeline) >= 8, 'timeline has per-loop samples', f'{len(timeline)} lines')
    for win in ('left', 'right'):
        check(results, any(e.get('window') == win and e.get('action') == 'match' for e in timeline),
              f'{win}: match action recorded in timeline')
        check(results, any(e.get('window') == win and e.get('confirmed') == 'match' for e in timeline),
              f'{win}: match confirmation recorded in timeline')
        before = next((e.get('before') for e in events
                       if e.get('window') == win and e.get('intent') == 'match'), None)
        check(results, bool(before) and (out/before).exists(), f'{win}: before-shot exists', str(before))
        after = next((e.get('after') for e in events
                      if e.get('window') == win and e.get('confirmed') == 'match'), None)
        check(results, bool(after) and (out/after).exists(), f'{win}: after-shot exists', str(after))
        check(results, (out/f'latest-{win}.png').exists(), f'{win}: latest frame exists')
        check(results, any(p.name.endswith(f'-{win}-ready.png') or p.name.endswith(f'-{win}-matching.png')
                           for p in out.glob('*.png')), f'{win}: evidence frame exists')

    mock_clicks = [json.loads(l) for l in clicks.read_text().splitlines()] if clicks.exists() else []
    for win in ('left', 'right'):
        check(results, any(c.get('window') == win and c.get('action') == 'match' for c in mock_clicks),
              f'{win}: match click reached the mock')

    print('viewer checks (offline replay of the finished run)…')
    viewer = subprocess.Popen([PY, str(SKILL/'viewer.py'), '--run-dir', str(out),
                               '--port', str(VIEWER_PORT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.2)
        base_url = f'http://127.0.0.1:{VIEWER_PORT}'
        try:
            with urllib.request.urlopen(base_url+'/api/data', timeout=10) as r:
                data = json.loads(r.read())
        except OSError as exc:
            check(results, False, 'viewer api reachable', str(exc))
            raise SystemExit(1 if not all(results) else 1)
        check(results, data['tl_total'] == len(timeline), 'viewer api timeline total', str(data['tl_total']))
        check(results, data['state']['status'] == 'complete', 'viewer api state readable')
        check(results, any('latest-left.png' == n for n in data['images']), 'viewer lists images')
        with urllib.request.urlopen(base_url+'/img/latest-left.png') as r:
            check(results, r.read()[:4] == b'\x89PNG', 'viewer serves frames')
        req = urllib.request.Request(base_url+'/api/stop', method='POST', data=b'')
        urllib.request.urlopen(req).read()
        with urllib.request.urlopen(base_url+'/api/data') as r:
            check(results, json.loads(r.read())['stopped'], 'viewer stop endpoint works')
    finally:
        viewer.terminate()
        viewer.wait(timeout=5)
    (out/'STOP').unlink(missing_ok=True)

    ok = all(results)
    print(f"\n{'E2E PASS' if ok else 'E2E FAIL'} — {sum(results)}/{len(results)} checks "
          f"(artifacts kept at {base if not ok else '…'})")
    if ok:
        subprocess.run(['rm', '-rf', str(base)])
    else:
        print(f'inspect: {base}')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
