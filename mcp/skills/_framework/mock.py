#!/usr/bin/env python3
"""Scenario-driven offline MCP mock shared by skill end-to-end tests.

Speaks the same tool surface as the fleet rustdesk MCP (screenshot / click /
keep_alive, each taking a leading device argument) over stdio. A scenario
JSON file declares reference images per state, click transitions (matched by
OCR — the mock runs the shared Vision OCR on the current state image and
finds the item containing the clicked point) and auto-transitions (timed,
with optional repeat counts, e.g. chained auto battles). Screenshots resize
the state image to the requested crop; the mild distortion is accepted —
this exercises the runner pipeline and its artifacts, recognition accuracy
is replay's job.

Usage: mock.py --scenario scenario.json   (or RUSTDESK_MOCK_SCENARIO env)
Clicks are appended to RUSTDESK_MOCK_CLICKS when set. Never touches a
device.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))  # mcp/skills, for _framework
from _framework.vision import ocr  # noqa: E402

from mcp.server.mcpserver import Image, MCPServer  # noqa: E402
from PIL import Image as PILImage  # noqa: E402


def load_scenario(path):
    data = json.loads(Path(path).read_text())
    data['images_dir'] = Path(data.get('images_dir', 'images')).resolve()
    data['native'] = tuple(data.get('native', [2400, 900]))
    data.setdefault('window_split', data['native'][0]//2)
    data.setdefault('start_state', {})
    data.setdefault('devices', {})   # name -> {native, window_split, start_state}
    return data


class WindowState:
    def __init__(self, scenario, name, start=None):
        self.scenario = scenario
        self.name = name
        starts = scenario['start_state'] if start is None else start
        self.state = starts.get(name.rsplit(':', 1)[-1], next(iter(scenario['states'])))
        self.entered_at = time.monotonic()
        self.pending = None       # (state, visible_at)
        self.auto_left = {}       # rule index -> remaining count

    @property
    def visible(self):
        if self.pending and time.monotonic() >= self.pending[1]:
            state, _ = self.pending
            self.pending = None
            self.state = state
            self.entered_at = time.monotonic()
        return self.state

    def image(self):
        spec = self.scenario['states'][self.visible]
        return PILImage.open(self.scenario['images_dir']/spec['image']).convert('RGB')

    def _enter(self, state, settle):
        self.pending = (state, time.monotonic()+settle)

    def click(self, nx, ny):
        """Resolve a click at normalized (nx, ny). Box transitions (known UI
        anchors) match geometrically first — deterministic and immune to OCR
        drift; text transitions fall back to OCR item containment."""
        state = self.visible
        for tr in self.scenario['transitions']:
            if tr.get('from') != state or 'box' not in tr:
                continue
            x, y, w, h = tr['box']
            if x <= nx <= x+w and y <= ny <= y+h:
                self._enter(tr['to'], tr.get('settle', 0))
                return tr.get('click_text') or 'box', tr['to']
        items = ocr(self.image())
        clicked = None
        for it in items:
            ex = it['w']*.3
            ey = it['h']*.3
            if (it['x']-ex <= nx <= it['x']+it['w']+ex
                    and it['y']-ey <= ny <= it['y']+it['h']+ey):
                clicked = it['text']
                break
        for tr in self.scenario['transitions']:
            if tr.get('from') != state or 'click_text' not in tr:
                continue
            if clicked is not None and tr['click_text'] in clicked:
                self._enter(tr['to'], tr.get('settle', 0))
                return clicked, tr['to']
        return clicked, None

    def auto(self):
        """Fire the first auto rule whose `after` elapsed and budget allows.
        Rules without `count` repeat forever."""
        state = self.visible
        for idx, rule in enumerate(self.scenario.get('auto', [])):
            if rule.get('from') != state:
                continue
            left = self.auto_left.get(idx, rule.get('count'))
            if left is not None and left <= 0:
                continue
            if time.monotonic()-self.entered_at < rule.get('after', 0):
                continue
            if left is not None:
                self.auto_left[idx] = left-1
            self._enter(rule['to'], rule.get('settle', 0))
            return rule['to']
        return None


_scenario_env = os.environ.get('RUSTDESK_MOCK_SCENARIO')
scenario = load_scenario(_scenario_env) if _scenario_env else None  # --scenario or bust
CLICKS = os.environ.get('RUSTDESK_MOCK_CLICKS', '')
WINDOWS: dict[str, WindowState] = {}
_last_crop: dict[str, tuple] = {}  # window -> (crop_x, crop_y, crop_w, crop_h)
_last_full: dict[str, bool] = {}   # device -> last screenshot was a full frame
_seq = 0

server = MCPServer(name='rustdesk-mock', version='0.1.0')


def window_for(device, x=0):
    """Left/right window within a device's own split."""
    spec = (scenario or {}).get('devices', {}).get(device) or {}
    return 'left' if x < spec.get('window_split', scenario['window_split']) else 'right'


def window_key(device, x=0):
    """State-namespace key: devices declared in scenario['devices'] keep
    their own window states; undeclared devices share the legacy left/right
    pair exactly as before."""
    win = window_for(device, x)
    return f'{device}:{win}' if device in scenario['devices'] else win


def serve(state: WindowState, size):
    global _seq
    _seq += 1
    state.auto()
    img = state.image().resize(size, PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def record(window, action, x, y, extra=''):
    if not CLICKS:
        return
    with open(CLICKS, 'a') as f:
        f.write(json.dumps(dict(time=time.time(), window=window, action=action,
                                x=x, y=y, extra=extra))+'\n')


@server.tool(structured_output=False)
async def screenshot(device: str = 'pc1', wait_for_new_frame: bool = False,
                     max_width: int = 1568, format: str = 'jpeg',
                     crop_x: int | None = None, crop_y: int | None = None,
                     crop_w: int | None = None, crop_h: int | None = None) -> list:
    """Scripted screenshot: current state image resized to the crop."""
    global _seq
    _seq += 1
    dspec = scenario['devices'].get(device)
    # A no-crop request on a device with a fullframe_state serves the whole
    # desktop composite (single-connection layouts: the member sweep reads
    # the full frame while leader windows use their own cropped states).
    full = (crop_x is None and crop_w is None and crop_h is None
            and dspec and 'fullframe_state' in dspec)
    _last_full[device] = bool(full)
    if full:
        win = f'{device}:full'
        ws = WINDOWS.setdefault(win, WindowState(
            scenario, 'full', start={'full': dspec['fullframe_state']}))
    else:
        win = window_key(device, crop_x or 0)
        ws = WINDOWS.setdefault(win, WindowState(
            scenario, win, start=(dspec or {}).get('start_state')))
    if crop_w or crop_h:
        size = (crop_w or 1200, crop_h or 900)
    elif dspec and 'native' in dspec:
        size = tuple(dspec['native'])   # full-frame requests use the device's own native
    else:
        size = (1200, 900)
    _last_crop[win] = (crop_x or 0, crop_y or 0, size[0], size[1])
    data = serve(ws, size)
    native = tuple(dspec['native']) if dspec and 'native' in dspec else scenario['native']
    note = (f'native_resolution={native[0]}x{native[1]} '
            f'image={size[0]}x{size[1]} frame_seq={_seq} stale=False mock={ws.visible}')
    return [Image(data=data, format='png'), note]


@server.tool()
async def click(device: str = 'pc1', x: int = 0, y: int = 0, button: str = 'left',
                double: bool = False, humanize: bool = True) -> str:
    """Resolve the click against the scenario and drive the state machine."""
    if _last_full.get(device):
        win = f'{device}:full'
    else:
        win = window_key(device, x)
    ws = WINDOWS.setdefault(win, WindowState(
        scenario, win.rsplit(':', 1)[-1],
        start=(scenario['devices'].get(device) or {}).get('start_state')))
    cx, cy, cw, ch = _last_crop.get(win, (0, 0, scenario['native'][0], scenario['native'][1]))
    nx, ny = (x-cx)/cw, (y-cy)/ch  # normalized within the last-served crop
    clicked, nxt = ws.click(nx, ny)
    action = f'{ws.visible}:{clicked}→{nxt}' if nxt else 'unresolved'
    record(win, action, x, y)
    return f'clicked {button} at ({x}, {y}) mock window={win} action={action}'


@server.tool()
async def scroll(device: str = 'pc1', x: int = 0, y: int = 0, dy: int = 0, dx: int = 0) -> str:
    """Recorded, no effect on the scenario (lists are visible by default)."""
    win = window_key(device, x)
    record(win, 'scroll', x, y, extra=f'dy={dy}')
    return f'scrolled ({dx}, {dy}) at ({x}, {y})'


@server.tool()
async def keep_alive(device: str = 'pc1', minutes: int = 30) -> str:
    """No-op lease renewal."""
    return f'renewed {device} for {minutes} minutes (mock)'


def main():
    global scenario
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', type=Path, required=scenario is None,
                        default=Path(_scenario_env) if _scenario_env else None)
    args = parser.parse_args()
    if args.scenario:
        scenario = load_scenario(args.scenario)
    server.run()  # stdio transport


if __name__ == '__main__':
    main()
