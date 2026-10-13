#!/usr/bin/env python3
"""Offline stdio MCP test: two independent clients, repeated 10/11-fight rounds.

The spec is constructed with an explicit mock.py path. No fleet fallback.
Reference crops and scenario files stay in a private temporary directory.
"""
import json
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
FRAMEWORK = ROOT/'mcp/skills/_framework'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, choices=(1, 2), default=2)
    parser.add_argument('--resume-task', action='store_true')
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix='zhuagui-e2e-'))
    images = work/'images'
    images.mkdir()
    for i in range(1, 10):
        source = Image.open(SKILL/'references/screens'/f'{i}.png').convert('RGB')
        source.crop((0, 25, source.width, source.height)).save(images/f'{i}.png')
    cfg = dict(device='pc1', calibrated=True, native_resolution=[1600, 600],
               navigation_delay=1, navigation_retry=3, navigation_timeout=150,
               autocal=False,  # mock frames are single-window crops, not a real desktop
               activity_list_scroll=[.65, .25, .22, .32], rng_seed=42,
               windows=[dict(name='left', identity='mock left', rect=[0, 0, 760, 570], rounds=args.rounds),
                        dict(name='right', identity='mock right', rect=[800, 0, 760, 570],
                             mode='double', double_points=40*args.rounds, reward_mode_confirmed=True)])
    scenario = dict(images_dir=str(images), native=[1600, 600], window_split=800,
                    start_state=dict(left='activity', right='claim'),
                    states=dict(activity=dict(image='1.png'), claim=dict(image='2.png'),
                                task=dict(image='3.png'), done=dict(image='6.png')),
                    transitions=[dict(from_='activity', box=[.05, .19, .14, .09], to='activity'),
                                 dict(from_='activity', box=[.84, .22, .09, .07], to='claim'),
                                 dict(from_='claim', box=[.76, .28, .16, .08], to='task'),
                                 dict(from_='task', box=[.77, .19, .18, .09], to='fight1'),
                                 dict(from_='task', box=[.26, .42, .09, .08], to='task'),  # dismiss NPC dialog
                                 dict(from_='done', box=[.54, .53, .12, .09], to='claim')], auto=[])
    for tr in scenario['transitions']:
        tr['from'] = tr.pop('from_')
    for i in range(1, 12):
        scenario['states'][f'fight{i}'] = dict(image='5.png')
        scenario['auto'].append({'from': f'fight{i}', 'after': 1.2,
                                 'to': f'fight{i+1}' if i < 11 else 'done'})
    if args.resume_task:
        cfg['native_resolution'] = [1600, 650]
        for win in cfg['windows']:
            win['rect'][1] = 30
        desktop = Image.new('RGB', (1600, 650), (15, 45, 130))
        draw = ImageDraw.Draw(desktop)
        font = ImageFont.truetype('/System/Library/Fonts/STHeiti Medium.ttc', 16)
        for x, source in ((0, '3.png'), (800, '2.png')):
            draw.rectangle((x, 0, x+759, 29), fill='white')
            draw.text((x+20, 3), '梦幻西游：时空', font=font, fill='black')
            with Image.open(images/source) as image:
                desktop.paste(image.resize((760, 570)), (x, 30))
        desktop.save(images/'desktop.png')
        scenario['native'] = [1600, 650]
        scenario['start_state']['left'] = 'task'
        scenario['devices'] = dict(pc1=dict(native=[1600, 650],
                                           start_state=scenario['start_state'],
                                           window_split=800, fullframe_state='desktop'))
        scenario['states']['desktop'] = dict(image='desktop.png')
    # Both windows exercise an 11-fight chain. Policy unit tests also cover ten.
    (work/'config.json').write_text(json.dumps(cfg))
    (work/'scenario.json').write_text(json.dumps(scenario))
    click_path = work/'clicks.jsonl'
    spec = dict(mcpServers=dict(rustdesk=dict(command=sys.executable,
             args=[str(SCRIPTS/'mock_zhuagui.py'), '--scenario', str(work/'scenario.json')],
             env=dict(RUSTDESK_MOCK_CLICKS=str(click_path)))))
    (work/'mock-spec.json').write_text(json.dumps(spec))
    command = [sys.executable, str(SCRIPTS/'zhuagui.py'), 'run', '--repo', str(ROOT),
               '--config', str(work/'config.json'), '--output', str(work/'run'),
               '--mcp-spec', str(work/'mock-spec.json'), '--execute']
    print(f'Mock only: {work}', flush=True)
    proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    state = json.loads((work/'run/state.json').read_text())
    assert proc.returncode == 0, (state, proc.stderr[-2000:])
    assert state['status'] == 'complete', state
    assert [s['count'] for s in state['windows']] == [args.rounds, args.rounds], state
    assert state['windows'][0]['estimated_spent'] == 0
    assert state['windows'][1]['estimated_spent'] == 40*args.rounds
    clicks = [json.loads(line) for line in click_path.read_text().splitlines()]
    moves = [c for c in clicks if c['action'] == 'mouse_move']
    clicks = [c for c in clicks if c['action'] != 'mouse_move']
    assert len(moves) == len(clicks), (moves, clicks)
    assert clicks and all(c['action'] != 'unresolved' for c in clicks), clicks
    events = [json.loads(line) for line in (work/'run/events.jsonl').read_text().splitlines()]
    for name in ('left', 'right'):
        actions = [e['sent'] for e in events if e.get('window') == name and 'sent' in e]
        expected_claims = args.rounds-int(args.resume_task and name == 'left')
        assert actions.count('claim_task') == expected_claims, actions
        assert actions.count('continue_round') == args.rounds-1, actions
        if args.rounds == 2:
            ended = next(e for e in events if e.get('window') == name
                         and e.get('observation', {}).get('state') == 'round_done' and e['count'] == 1)
            continued = next(e for e in events if e.get('window') == name and e.get('sent') == 'continue_round')
            assert 10 <= ended['continue_delay'] <= 40, ended
            assert continued['time']-ended['time'] >= ended['continue_delay'], (ended, continued)
        assert actions.count('goto_ghost') == args.rounds, actions
        assert actions.count('dismiss_dialog') == args.rounds, actions  # once per claim
        assert set(actions) <= {'select_daily', 'join_entry', 'claim_task', 'dismiss_dialog',
                                'continue_round', 'goto_ghost'}, actions
        assert not (work/f'run/latest-{name}.png').exists()
        assert (work/f'run/recognition-{name}.json').exists()
    assert (work/'run/events.jsonl').exists()
    assert (work/'run/timeline.jsonl').exists()
    assert all(p.name.endswith(('-before.png', '-after.png')) for p in (work/'run').glob('*.png'))
    assert len(list((work/'run').glob('*-before.png'))) >= len(clicks)
    assert len(list((work/'run').glob('*-after.png'))) >= len(clicks)
    # A second run in observe mode must issue no input and respond to STOP.
    observe = work/'observe'
    command = [arg for arg in command if arg != '--execute']
    command[command.index('--output')+1] = str(observe)
    previous_clicks = click_path.read_text()
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        end = time.monotonic()+20
        while not (observe/'state.json').exists() and time.monotonic() < end:
            if proc.poll() is not None:
                raise AssertionError(proc.stderr.read())
            time.sleep(.1)
        assert (observe/'state.json').exists(), 'observe did not initialize'
        (observe/'STOP').touch()
        _, error = proc.communicate(timeout=20)
        assert proc.returncode == 0, error
        assert json.loads((observe/'state.json').read_text())['status'] == 'stopped'
        assert click_path.read_text() == previous_clicks, 'observe sent input'
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=20)
    print(json.dumps(dict(status='passed', directory=str(work), windows=[s['count'] for s in state['windows']],
                          clicks=len(clicks), moves=len(moves), double_spent=40*args.rounds, observe_stop=True)), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
