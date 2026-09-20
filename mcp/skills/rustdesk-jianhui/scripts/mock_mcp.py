#!/usr/bin/env python3
"""Offline MCP mock for jianhui end-to-end runs. Never touches a device.

Speaks the same tool surface as the fleet rustdesk MCP (screenshot / click /
keep_alive, each taking a leading device argument) over stdio. Screenshots
come from the repo reference images, resized to the requested crop so the
runner's capture() size checks pass. match clicks start a scripted game
(matching -> unknown -> battle -> result -> room); clicks are recorded to a
JSONL. This exercises the runner pipeline and its artifacts only —
recognition accuracy is replay.py's job, and the mild resize distortion is
accepted for that reason.
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage

IMAGES = Path(os.environ.get('JIANHUI_MOCK_IMAGES', 'images'))
CLICKS = os.environ.get('JIANHUI_MOCK_CLICKS', '')
NATIVE = tuple(int(v) for v in os.environ.get('JIANHUI_MOCK_NATIVE', '2400x900').split('x'))
RIGHT_OFFSET = float(os.environ.get('JIANHUI_MOCK_RIGHT_OFFSET', '2'))
START = time.monotonic()

SLOTS = [[.20, .65, .16, .15], [.37, .68, .16, .15], [.56, .65, .16, .15], [.75, .62, .17, .16]]
LOBBY_ROOM = '匹配界面单个.png'
# (image, seconds) after a match click; falls back to the lobby room afterwards.
GAME = [('匹配中.png', 6), ('匹配成功.png', 4), ('战斗ing.png', 8), ('战斗失败.png', 8)]
READY = '·synthetic-ready·'  # lobby room with slot-0's 准备 mark copied into slots 1/2

server = MCPServer(name='jianhui-mock', version='0.1.0')

_game_start: dict[str, float] = {}
_png_cache: dict[tuple, bytes] = {}
_seq = 0


def _synthetic_ready() -> PILImage.Image:
    img = PILImage.open(IMAGES/LOBBY_ROOM).convert('RGB')
    w, h = img.size

    def rect(slot, pad=1.15):
        cx, cy = (slot[0]+slot[2]/2)*w, (slot[1]+slot[3]/2)*h
        pw, ph = slot[2]*w*pad/2, slot[3]*h*pad/2
        return round(cx-pw), round(cy-ph), round(cx+pw), round(cy+ph)

    mark = img.crop(rect(SLOTS[0]))
    for slot in SLOTS[1:3]:
        r = rect(slot)
        pos = (round((r[0]+r[2])/2-mark.width/2), round((r[1]+r[3])/2-mark.height/2))
        img.paste(mark, pos)
    return img


def _source(name):
    if name == READY:
        return _synthetic_ready()
    return PILImage.open(IMAGES/name).convert('RGB')


def _png(name, size):
    key = (name, size)
    if key not in _png_cache:
        out = io.BytesIO()
        _source(name).resize(size, PILImage.LANCZOS).save(out, format='PNG')
        _png_cache[key] = out.getvalue()
    return _png_cache[key]


def _window(crop_x):
    return 'left' if (crop_x or 0) < NATIVE[0]/2 else 'right'


def _scenario_name(window):
    start = _game_start.get(window)
    if start is not None:
        elapsed = time.monotonic()-start
        for name, duration in GAME:
            if elapsed < duration:
                return name
            elapsed -= duration
        return LOBBY_ROOM  # game over; back to the lobby
    t = time.monotonic()-START-(RIGHT_OFFSET if window == 'right' else 0)
    return LOBBY_ROOM if t < 6 else READY


def _record(window, x, y, action):
    if not CLICKS:
        return
    with open(CLICKS, 'a') as f:
        f.write(json.dumps(dict(time=time.time(), window=window, x=x, y=y, action=action))+'\n')


@server.tool(structured_output=False)
async def screenshot(device: str = 'pc1', wait_for_new_frame: bool = False,
                     max_width: int = 1568, format: str = 'jpeg',
                     crop_x: int | None = None, crop_y: int | None = None,
                     crop_w: int | None = None, crop_h: int | None = None) -> list:
    """Scripted screenshot: reference image resized to the requested crop."""
    global _seq
    _seq += 1
    window = _window(crop_x)
    name = _scenario_name(window)
    size = (crop_w or 1200, crop_h or 900)
    data = _png(name, size)
    note = (f'native_resolution={NATIVE[0]}x{NATIVE[1]} image={size[0]}x{size[1]} '
            f'frame_seq={_seq} stale=False mock={name}')
    return [Image(data=data, format='png'), note]


@server.tool()
async def click(device: str = 'pc1', x: int = 0, y: int = 0, button: str = 'left',
                double: bool = False, humanize: bool = True) -> str:
    """Record the click and drive the scenario: match on the ready screen."""
    window = _window(x)
    name = _scenario_name(window)
    if name == READY:
        _game_start[window] = time.monotonic()
        action = 'match'
    elif name == '结算完成界面.png':
        action = 'close'
    else:
        action = 'observe'
    _record(window, x, y, action)
    return f'clicked {button} at ({x}, {y}) mock window={window} action={action}'


@server.tool()
async def keep_alive(device: str = 'pc1', minutes: int = 30) -> str:
    """No-op lease renewal."""
    return f'renewed {device} for {minutes} minutes (mock)'


if __name__ == '__main__':
    server.run()  # stdio transport
