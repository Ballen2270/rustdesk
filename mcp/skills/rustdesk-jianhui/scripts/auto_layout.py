#!/usr/bin/env python3
"""Calibrate jianhui window rects/slots/bands from one full-desktop frame.

Detects every game window (title-bar OCR anchors), measures the ladder button,
member name row and owner mark inside each room window, and emits a runner
config. Refuses overlapping windows. Live mode grabs a native-resolution frame
through the rustdesk MCP; --image mode works offline (downsampled JPEGs may
miss small title text — prefer native screenshots).

  auto_layout.py --repo $REPO --device pc1 --keyword 雾上青天 --keyword 剑霜九洲 [--out cfg.json]
  auto_layout.py --image full.png --native 1920x1080 --keyword A --keyword B [--out cfg.json]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import re
import sys
from pathlib import Path

from PIL import Image

sys.path.append(str(Path(__file__).resolve().parents[2]))  # mcp/skills, for _framework
sys.path.append(str(Path(__file__).resolve().parent))
from _framework.vision import ocr, region_ocr, green_label  # noqa: E402
from jianhui import DEFAULT_BANDS, merge_bands, recognize, validate  # noqa: E402

TITLE_TEXT = '梦幻西游'
TITLE_TOP_PAD = 10       # window top sits ~10px above the title text box
TASKBAR_SCAN = 0.12      # bottom fraction scanned for the taskbar edge
ATTRIBUTE_WORDS = {'防御', '伤害', '速度', '队长', '等级', '门派', '房主', '准备', '取消'}


def find_taskbar_frac(img):
    """Fractional y of the strongest horizontal edge near the screen bottom."""
    w, h = img.size
    px = img.load()
    y0 = int(h*(1-TASKBAR_SCAN))
    best_d = 0.0
    frac = 1.0
    for y in range(y0+1, h):
        d = sum(abs(sum(px[x, y])-sum(px[x, y-1]))/3 for x in range(0, w, 8))/max(1, len(range(0, w, 8)))
        if d > best_d:
            best_d, frac = d, y/h
    return frac if best_d > 8 else 1.0


def snap_column(img, native_w, guess, radius=50):
    """Snap a guessed native x to the strongest vertical edge within ±radius."""
    w, h = img.size
    px = img.load()
    lo, hi = max(1, int((guess-radius)*w/native_w)), min(w, int((guess+radius)*w/native_w))
    best_x, best_d = int(guess*w/native_w), 0.0
    for x in range(lo, hi):
        d = sum(abs(sum(px[x, y])-sum(px[x-1, y]))/3 for y in range(int(h*.3), int(h*.8), 4))
        if d > best_d:
            best_d, best_x = d, x
    return round(best_x*native_w/w)


def title_band_extent(img, native_w, native_h, anchor):
    """Light title-bar band covering the anchor on its own row, in native x.

    Active windows show a white title bar spanning the window width; returns
    (start, end) or None when the row is not mostly light at the anchor."""
    w, h = img.size
    px = img.load()
    y = int(anchor['y']*h)
    light = lambda x: sum(px[x, y])/3 > 170
    if not (light(int(anchor['x']*w)) or light(min(w-1, int((anchor['x']+anchor['w'])*w)-1))):
        return None
    x0 = int(anchor['x']*w)
    while x0 > 0 and light(x0-1):
        x0 -= 1
    x1 = min(w-1, int((anchor['x']+anchor['w'])*w))
    while x1 < w-1 and light(x1+1):
        x1 += 1
    if x1-x0 < .1*w:  # a word, not a bar
        return None
    return (round(x0*native_w/w), round(x1*native_w/w))


def detect_windows(img, native_w, native_h, expect):
    """Cluster title anchors into windows; derive rects from screen structure."""
    items = ocr(img)
    anchors = [it for it in items if TITLE_TEXT in it['text'] and it['score'] >= .3]
    if len(anchors) < expect:
        raise SystemExit(json.dumps({
            'error': f'found {len(anchors)}/{expect} window titles',
            'hint': 'titles may be too small (downsampled image) or covered; '
                    'use a native-resolution screenshot, tile overlapping windows',
            'titles_found': [{'x': round(it['x']*native_w), 'y': round(it['y']*native_h)} for it in anchors]},
            ensure_ascii=False))
    cols, rows = [], []
    for it in sorted(anchors, key=lambda a: (round(a['y'], 1), a['x'])):
        cx, cy = it['x']*native_w, it['y']*native_h
        if not any(abs(cx-c) < 120 for c in cols):
            cols.append(cx)
        if not any(abs(cy-r) < 120 for r in rows):
            rows.append(cy)
    cols.sort()
    rows.sort()
    bottom_frac = find_taskbar_frac(img)
    rects = []
    for anchor in sorted(anchors, key=lambda a: (a['y'], a['x'])):
        cx, cy = anchor['x']*native_w, anchor['y']*native_h
        band = title_band_extent(img, native_w, native_h, anchor)
        if band:  # active window: bar ends are the window sides (icons may cut
            x0, x1 = band  # the bar short of the real right border -> resnap)
            x0 = snap_column(img, native_w, x0, 30) if x0 > 40 else 0
            rightmost = not any(o['x'] > anchor['x']+.1 for o in anchors)
            if rightmost and x1 < native_w-100:
                x1 = snap_column(img, native_w, native_w, 60)
            else:
                x1 = snap_column(img, native_w, x1, 30)
        else:     # dark title bar: fall back to content-column edges near guesses
            x0 = snap_column(img, native_w, cx-20) if cx > 40 else 0
            x1 = snap_column(img, native_w, native_w)
            for other in anchors:
                ox = other['x']*native_w
                if cx+120 < ox < x1:
                    x1 = snap_column(img, native_w, ox-20, 40)
        next_row = min([r for r in rows if r > cy+120] + [native_h*bottom_frac])
        y0 = max(0, int(cy+anchor['h']*native_h+8))  # game content starts under the title bar
        y1 = int(next_row-2)
        if any(r[0] == x0 and r[1] == y0 for r in rects):
            continue
        rects.append([int(x0), int(y0), int(x1-x0), int(y1-y0)])
    # overlap refusal: any two rects sharing >15% of the smaller area
    for i in range(len(rects)):
        for j in range(i+1, len(rects)):
            a, b = rects[i], rects[j]
            ox = max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0]))
            oy = max(0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
            if ox*oy > .15*min(a[2]*a[3], b[2]*b[3]):
                raise SystemExit(json.dumps({
                    'error': 'windows overlap; arrange them tiled before calibrating',
                    'rects': rects}, ensure_ascii=False))
    return rects, items


def window_crop(img, rect, native_w, native_h):
    x, y, w, h = rect
    fw, fh = img.size
    return img.crop((int(x*fw/native_w), int(y*fh/native_h),
                     int((x+w)*fw/native_w), int((y+h)*fh/native_h)))


def fuzzy_in(keyword, text):
    """True when keyword appears in text with at most one wrong glyph (OCR)."""
    if keyword in text:
        return True
    if len(keyword) < 3:  # short names must match exactly; fuzzy is too loose
        return False
    if len(text) < len(keyword):
        keyword, text = text, keyword
    n = len(keyword)
    for i in range(len(text)-n+1):
        window = text[i:i+n+1]
        diffs = sum(1 for a, b in zip(keyword, window) if a != b)
        if diffs <= 1 and abs(len(window)-n) <= 1:
            return True
    return False


def measure_room(crop, keyword):
    """OCR-measure button/name-row/owner geometry; returns (bands, slots, matched).

    matched is True only when the room title carries the requested keyword —
    i.e. this crop is that team's owner window."""
    items = ocr(crop)
    button = next((it for it in items if '天梯匹配' in it['text'] or '取消匹配' in it['text']), None)
    room_title = next((it for it in items if '的房间' in it['text']), None)
    matched = bool(room_title) and fuzzy_in(keyword, room_title['text'])
    if not room_title:
        return None, None, False
    # member name row: CJK labels on one horizontal line in the lower half,
    # attribute words excluded; keep the largest y-consistent cluster
    cjk = lambda t: re.sub(r'[^\u4e00-\u9fff]', '', t)
    labels = [it for it in items
              if .40 <= it['y'] <= .62 and 2 <= len(cjk(it['text'])) <= 5
              and cjk(it['text']) not in ATTRIBUTE_WORDS
              and it['score'] >= .3]
    clusters = []
    for it in sorted(labels, key=lambda i: i['y']):
        for c in clusters:
            if abs(c[0]['y']-it['y']) < .04:
                c.append(it)
                break
        else:
            clusters.append([it])
    row = max(clusters, key=len) if clusters else []
    row.sort(key=lambda i: i['x'])
    merged = []
    for n in row:  # drop overlapping reads of the same label
        if not merged or n['x']-merged[-1]['x'] > .10:
            merged.append(n)
    owner_mark = next((it for it in items if '房主' in it['text']), None)
    bands, slots = {}, None
    if button:
        bx, by = button['x']+button['w']/2, button['y']+button['h']/2
        bands['bottom'] = [round(min(max(0, bx-.08), 1-.22), 3), round(max(0, by-.06), 3), .22, .13]
        bands['battle_cancel'] = [round(min(max(0, bx-.02), 1-.2), 3), round(max(0, by-.10), 3), .2, .38]
    if merged:
        name_y = merged[0]['y']
        bands['room_band'] = [0, round(max(0, name_y-.09), 3), .98, .34]
        bands['ready_y'] = [round(max(0, name_y-.03), 3), round(name_y+.13, 3)]
        owner_y = owner_mark['y'] if owner_mark else name_y
        bands['owner'] = [0, round(max(0, min(name_y, owner_y)-.09), 3), 1, .45]
    if matched and len(merged) >= 4:
        centers = sorted(n['x']+n['w']/2 for n in merged)
        if owner_mark:
            own_cx = owner_mark['x']+owner_mark['w']/2
        else:  # owner mark unreadable: locate the owner by their own name
            own = next((n for n in merged if fuzzy_in(keyword, n['text'])), None)
            own_cx = own['x']+own['w']/2 if own else None
        if own_cx is not None:
            member_cx = [c for c in centers if abs(c-own_cx) > .07]
            if len(member_cx) == 4:
                slots = [[round(max(0, c-.09), 3), round(max(0, merged[0]['y']-.02), 3), .18, .13]
                         for c in member_cx]
    return bands, slots, matched


def build_config(frame, native_w, native_h, keywords, expect):
    rects, _ = detect_windows(frame, native_w, native_h, expect)
    report = {'native_resolution': [native_w, native_h], 'windows': [], 'warnings': []}
    owner_windows = []
    for rect in rects:
        crop = window_crop(frame, rect, native_w, native_h)
        obs, _ = recognize(crop)
        entry = {'rect': rect, 'state': obs['state']}
        for kw in keywords:
            bands, slots, matched = measure_room(crop, kw)
            if matched:
                entry.update(keyword=kw, bands=bands or {}, slots=slots)
                owner_windows.append(entry)
                break
        if 'keyword' not in entry:
            report['warnings'].append(f"rect {rect}: state {obs['state']}, no room title matched; skipped")
        report['windows'].append(entry)
    for e in report['windows']:
        x, y, w, h = e['rect']
        e['aspect'] = round(w/h, 3)
        if not 1.2 <= w/h <= 1.45:
            report['warnings'].append(
                f"rect {e['rect']}: aspect {w/h:.2f} far from the usual 4:3 game window; verify the rect")
    if len(owner_windows) != 2:
        raise SystemExit(json.dumps({'error': f'expected 2 owner windows, matched {len(owner_windows)}',
                                     **report}, ensure_ascii=False))
    owner_windows.sort(key=lambda e: e['rect'][0])
    windows = []
    for name, entry in zip(('left', 'right'), owner_windows):
        win = {'name': name, 'rect': entry['rect'], 'room_keyword': entry['keyword']}
        if entry.get('slots'):
            win['ready_slots'] = entry['slots']
        windows.append(win)
    shared = merge_bands({k: v for e in owner_windows for k, v in e.get('bands', {}).items()})
    if not all(e.get('slots') for e in owner_windows):
        report['warnings'].append('slots unmeasured on some window (not in room or owner mark hidden); '
                                  'fill ready_slots manually before running')
    cfg = {'device': 'pc1', 'calibrated': True, 'native_resolution': [native_w, native_h],
           'initial_counts': [0, 0], 'windows': windows,
           'bands': {k: list(v) for k, v in shared.items() if list(v) != list(DEFAULT_BANDS[k])},
           'panel_close': [.904, .139], 'room_timeout': 120}
    validate(cfg, 10)  # self-check the emitted config shape
    report['config'] = cfg
    return report


async def grab_frame(repo, device):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os
    spec = json.loads((repo/'.mcp.json').read_text())['mcpServers']['rustdesk']
    params = StdioServerParameters(command=spec['command'], args=spec['args'], cwd=str(repo),
                                   env={**os.environ, **spec.get('env', {}), 'RUSTDESK_SCREENSHOT_DIR': ''})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), 30)
            result = await asyncio.wait_for(
                session.call_tool('screenshot', {'device': device, 'format': 'png', 'max_width': 2560}), 12)
            if result.is_error:
                raise SystemExit(f'screenshot failed: {result.content}')
            blob = next(c.data for c in result.content if c.type == 'image')
            return Image.open(io.BytesIO(base64.b64decode(blob))).convert('RGB')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--image', type=Path, help='offline full-desktop frame')
    ap.add_argument('--native', default='1920x1080', help='native resolution WxH of --image')
    ap.add_argument('--repo', type=Path, help='repo root for live MCP capture')
    ap.add_argument('--device', default='pc1')
    ap.add_argument('--keyword', action='append', required=True, help="owner name, repeat per window")
    ap.add_argument('--expect-windows', type=int, default=2,
                    help='how many game windows the desktop has (title-anchor count is checked against it)')
    ap.add_argument('--out', type=Path, help='write generated config here')
    args = ap.parse_args()
    if args.image:
        frame = Image.open(args.image).convert('RGB')
        w, h = map(int, args.native.split('x'))
    elif args.repo:
        w, h = 1920, 1080
        frame = asyncio.run(grab_frame(args.repo, args.device))
        w, h = frame.size if frame.size[0] > 1600 else (1920, 1080)
    else:
        ap.error('need --image or --repo')
    report = build_config(frame, w, h, args.keyword, args.expect_windows)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(json.dumps(report['config'], ensure_ascii=False, indent=2)+'\n')
        report['written'] = str(args.out)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(text)


if __name__ == '__main__':
    main()
