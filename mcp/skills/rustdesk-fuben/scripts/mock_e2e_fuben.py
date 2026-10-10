#!/usr/bin/env python3
"""Offline stdio MCP test: two leader windows, one knight run + one normal run
each, driven through cropped reference frames — with the member ready popups
living on three extra mock devices (pc1d1/pc2/pc3, popup windows at 960/850/
615px). The runner itself must find and click 准备 on every member device
while the leader waits; the wait screen only auto-advances 8s after the queue
(unreadied members fall back to the requeue path, unit-tested separately)."""
import json
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
from fuben import detect_ready_popups  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix='fuben-e2e-'))
    images = work/'images'
    images.mkdir()
    for i in range(1, 25):
        source = Image.open(SKILL/'references/screens'/f'{i}.png').convert('RGB')
        source.crop((0, 25, source.width, source.height)).save(images/f'{i}.png')
    Image.open(SKILL/'references/screens'/'26.png').convert('RGB').crop(
        (0, 25, 769, 602)).save(images/'26.png')

    def paste_window(canvas, name, x0, y0, width):
        src = Image.open(images/name).convert('RGB')
        src = src.resize((width, round(src.height*width/src.width)), Image.LANCZOS)
        canvas.paste(src, (x0, y0))

    # Member device frames: one ready-popup window plus a city decoy, at the
    # window widths measured live (pc2 ~850, pc3 ~615 — the smallest, where
    # the button OCRs as 淮备 and only the pair rule holds). pc1 carries its
    # members ON the leader device (single-connection layout): its full-frame
    # composite is the desktop with the popup over the right-hand window.
    member_popups = {}
    for dev, width in (('pc2', 850), ('pc3', 615)):
        canvas = Image.new('RGB', (1920, 1080), (28, 34, 40))
        paste_window(canvas, '7.png', 0, 0, width)
        paste_window(canvas, '1.png', 1000, 150, 680)
        canvas.save(images/f'm_pop_{dev}.png')
        buttons, _ = detect_ready_popups(canvas)
        assert len(buttons) == 1, (dev, width, 'popup not detected at setup')
        member_popups[dev] = buttons[0]
    pc1_full = Image.new('RGB', (1600, 600), (28, 34, 40))
    paste_window(pc1_full, '1.png', 0, 5, 780)
    paste_window(pc1_full, '1.png', 810, 5, 780)
    pc1_city = pc1_full.copy()
    pc1_city.save(images/'m_city_pc1.png')
    paste_window(pc1_full, '7.png', 880, 80, 600)
    pc1_full.save(images/'m_pop_pc1.png')
    buttons, _ = detect_ready_popups(pc1_full)
    assert len(buttons) == 1, ('pc1', 'fullframe popup not detected at setup')
    member_popups['pc1'] = buttons[0]
    city2 = Image.new('RGB', (1920, 1080), (28, 34, 40))
    paste_window(city2, '1.png', 0, 0, 960)
    paste_window(city2, '1.png', 1000, 150, 680)
    city2.save(images/'m_city.png')

    cfg = dict(device='pc1', calibrated=True, native_resolution=[1600, 600],
               autocal=False, navigation_delay=1, knight_runs=1, normal_runs=1,
               activity_list_scroll=[.65, .25, .22, .32], rng_seed=7,
               members=['pc1', 'pc2', 'pc3'],
               windows=[dict(name='left', identity='mock left', rect=[0, 0, 760, 570]),
                        dict(name='right', identity='mock right', rect=[800, 0, 760, 570])])
    battle = '11.png'
    scenario = dict(images_dir=str(images), native=[1600, 600], window_split=800,
                    start_state=dict(left='city', right='city'),
                    devices=dict(pc1=dict(native=[1600, 600], window_split=800,
                                          start_state=dict(left='city', right='city'),
                                          fullframe_state='m_pop_pc1'),
                                 pc2=dict(native=[1920, 1080], window_split=1920,
                                          start_state=dict(left='m_pop_pc2')),
                                 pc3=dict(native=[1920, 1080], window_split=1920,
                                          start_state=dict(left='m_pop_pc3'))),
                    states=dict(city=dict(image='1.png'),
                                activity=dict(image='2.png'),
                                baixiao=dict(image='3.png'),
                                panel_k=dict(image='5.png'),
                                panel_n=dict(image='4.png'),
                                wait=dict(image='6.png'),
                                m8=dict(image='8.png'), m9=dict(image='9.png'),
                                m10=dict(image='10.png'), m11=dict(image=battle),
                                m12=dict(image='12.png'), m13=dict(image='13.png'),
                                m14=dict(image='14.png'), m15=dict(image=battle),
                                m16=dict(image='16.png'), m17=dict(image='17.png'),
                                m18=dict(image='18.png'), m19b=dict(image=battle),
                                m19=dict(image='19.png'),
                                reward=dict(image='20.png'), done=dict(image='21.png'),
                                m_city=dict(image='m_city.png'),
                                m_city_pc1=dict(image='m_city_pc1.png'),
                                m_pop_pc1=dict(image='m_pop_pc1.png'),
                                m_pop_pc2=dict(image='m_pop_pc2.png'),
                                m_pop_pc3=dict(image='m_pop_pc3.png')),
                    transitions=[
                        dict(from_='city', box=[.20, 0, .20, .12], to='activity'),
                        dict(from_='activity', box=[.03, .17, .18, .10], to='activity'),
                        dict(from_='activity', box=[.48, .495, .10, .05], to='baixiao'),
                        dict(from_='activity', box=[.83, .495, .10, .05], to='baixiao'),
                        dict(from_='baixiao', box=[.70, .43, .27, .09], to='panel_k'),
                        dict(from_='panel_k', box=[.13, .18, .16, .09], to='panel_n'),
                        dict(from_='panel_k', box=[.29, .18, .16, .09], to='panel_k'),
                        dict(from_='panel_n', box=[.29, .18, .16, .09], to='panel_k'),
                        dict(from_='panel_k', box=[.20, .74, .16, .09], to='wait'),
                        dict(from_='panel_n', box=[.20, .74, .16, .09], to='m8'),
                        dict(from_='m8', box=[.79, .30, .20, .10], to='m9'),
                        dict(from_='m9', box=[.76, .19, .24, .11], to='m10'),
                        dict(from_='m10', box=[.68, .66, .29, .16], to='m11'),
                        dict(from_='m12', box=[.79, .30, .20, .10], to='m13'),
                        dict(from_='m13', box=[.76, .19, .24, .11], to='m14'),
                        dict(from_='m14', box=[.68, .66, .29, .16], to='m15'),
                        dict(from_='m16', box=[.79, .30, .20, .10], to='m17'),
                        dict(from_='m17', box=[.76, .19, .24, .11], to='m18'),
                        dict(from_='m18', box=[.68, .66, .29, .16], to='m19b'),
                        dict(from_='m19', box=[.79, .30, .20, .10], to='reward'),
                        dict(from_='reward', box=[.36, .73, .28, .10], to='done'),
                        dict(from_='done', box=[.76, .24, .23, .09], to='city')]
                    + [dict(from_=f'm_pop_{dev}',
                            box=[b['x'], b['y'], b['w'], b['h']],
                            to='m_city_pc1' if dev == 'pc1' else 'm_city')
                       for dev, b in member_popups.items()],
                    auto=[dict(**{'from': 'wait'}, after=8.0, to='m8'),
                          dict(**{'from': 'm11'}, after=1.5, to='m12'),
                          dict(**{'from': 'm15'}, after=1.5, to='m16'),
                          dict(**{'from': 'm19b'}, after=1.5, to='m19')])
    for tr in scenario['transitions']:
        tr['from'] = tr.pop('from_')
    (work/'config.json').write_text(json.dumps(cfg))
    (work/'scenario.json').write_text(json.dumps(scenario))
    click_path = work/'clicks.jsonl'
    spec = dict(mcpServers=dict(rustdesk=dict(command=sys.executable,
             args=[str(SCRIPTS/'mock_fuben.py'), '--scenario', str(work/'scenario.json')],
             env=dict(RUSTDESK_MOCK_CLICKS=str(click_path)))))
    (work/'mock-spec.json').write_text(json.dumps(spec))
    command = [sys.executable, str(SCRIPTS/'fuben.py'), 'run', '--repo', str(ROOT),
               '--config', str(work/'config.json'), '--output', str(work/'run'),
               '--mcp-spec', str(work/'mock-spec.json'), '--execute', '--no-viewer']
    print(f'Mock only: {work}', flush=True)
    proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
    state = json.loads((work/'run/state.json').read_text())
    assert proc.returncode == 0, (state, proc.stderr[-2000:])
    assert state['status'] == 'complete', state['status']
    for win in state['windows']:
        assert win['knight'] == 1 and win['normal'] == 1, win
    # every member device was readied by the sweep itself — pc1 shares the
    # leader device (full-frame composite), pc3 is the 615px window whose
    # button OCRs as 淮备 and only the pair rule finds it
    for dev in ('pc1', 'pc2', 'pc3'):
        assert state['members'][dev]['clicked'] >= 1, state['members']
    events = [json.loads(l) for l in (work/'run/events.jsonl').read_text().splitlines()]
    for name in ('left', 'right'):
        sent = [e['intent'] for e in events if e.get('window') == name and 'intent' in e]
        assert 'skip_story' in sent and 'dialog_confirm' in sent and 'finish_dungeon' in sent, sent
        assert sent.count('enter_dungeon') == 2, sent
        assert 'select_daily' in sent and 'join_dungeon' in sent, sent
    clicks = [json.loads(l) for l in click_path.read_text().splitlines()]
    member_clicks = [c for c in clicks
                     if c['window'] in ('pc1:full', 'pc2:left', 'pc3:left')
                     and 'm_city' in c['action']]
    assert len(member_clicks) >= 3, clicks
    # and they happened before the first leader left the ready screen (the
    # wait auto-advances 8s after the queue; sweeps must land well inside)
    skips = [c for c in clicks
             if c['window'] in ('pc1:left', 'pc1:right') and 'm9' in c['action']]
    assert skips and max(c['time'] for c in member_clicks) < min(c['time'] for c in skips), \
        'members were not readied before the dungeon entry'
    # skip_story mashes 2-4 presses: the first fires the transition and the
    # rest land on the next screen as immediate same-point repeats — legal
    # by design. Any other unresolved click still fails the run.
    resolved, legal = None, True
    for c in clicks:
        if c['action'] == 'unresolved':
            if not (resolved and c['time']-resolved['time'] <= 2.5
                    and abs(c['x']-resolved['x']) <= 6
                    and abs(c['y']-resolved['y']) <= 6):
                legal = False
                break
        else:
            resolved = c
    assert clicks and legal, clicks
    # observe mode sends nothing; STOP is honored
    obs_proc = subprocess.Popen([sys.executable, str(SCRIPTS/'fuben.py'), 'run', '--repo', str(ROOT),
                                 '--config', str(work/'config.json'), '--output', str(work/'observe'),
                                 '--mcp-spec', str(work/'mock-spec.json'), '--no-viewer'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(6)
    (work/'observe/STOP').touch()
    obs_proc.wait(timeout=60)
    observe_clicks = [json.loads(l) for l in click_path.read_text().splitlines()][len(clicks):]
    assert not observe_clicks, observe_clicks
    stopped = json.loads((work/'observe/state.json').read_text())
    assert stopped['status'] in ('stopped', 'complete'), stopped['status']
    print(json.dumps(dict(status='passed', directory=str(work),
                          windows=[(w['knight'], w['normal']) for w in state['windows']],
                          clicks=len(clicks), observe_stop=True)))


if __name__ == '__main__':
    raise SystemExit(main())
