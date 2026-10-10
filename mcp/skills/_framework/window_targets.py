"""Discover and select game windows locally from RustDesk screenshots.

Existing calibrated configs keep their original startup path. No Windows
commands or agent are used. New selections are re-discovered from a fresh frame
and matched by visible account/room identity before activity inputs.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import importlib.util
import io
import json
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image

if __package__:
    from .vision import region_ocr, text_hits
else:
    from vision import region_ocr, text_hits

ACTIVITIES = ('zhuagui', 'fuben', 'menpai', 'mijing', 'jianhui')


def read_inventory(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    device = data.get('device')
    if not isinstance(device, str) or not re.fullmatch(r'[a-z0-9_-]+', device):
        raise ValueError('Invalid inventory device')
    resolution = data.get('native_resolution')
    if (not isinstance(resolution, list) or len(resolution) != 2
            or any(type(v) is not int or v <= 0 for v in resolution)):
        raise ValueError('Invalid inventory native_resolution')
    windows = data.get('windows')
    if not isinstance(windows, list) or not windows:
        raise ValueError('Inventory has no visible game windows')
    ids = set()
    for win in windows:
        key = win.get('id')
        if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9_-]+', key) or key in ids:
            raise ValueError('Inventory window IDs must be distinct')
        ids.add(key)
        rect = win.get('rect')
        if (not isinstance(rect, list) or len(rect) != 4
                or any(type(v) is not int for v in rect)):
            raise ValueError('Invalid inventory client rectangle')
        x, y, w, h = rect
        if min(x, y) < 0 or min(w, h) < 300 or x+w > resolution[0] or y+h > resolution[1]:
            raise ValueError('Inventory window is clipped or too small')
    return data


def overlaps(a, b):
    return (max(a[0], b[0]) < min(a[0]+a[2], b[0]+b[2])
            and max(a[1], b[1]) < min(a[1]+a[3], b[1]+b[3]))


def require_uncovered(window, inventory):
    for other in inventory['windows']:
        if other['id'] != window['id'] and overlaps(window['rect'], other['rect']):
            raise ValueError('Tile overlapping windows before selecting '+inventory['device']+':'+window['id'])


def build_config(template, inventory_paths, targets, identities, activity):
    if activity not in ACTIVITIES:
        raise ValueError('Unknown activity')
    if (activity == 'jianhui' and len(targets) != 2
            or activity in ('zhuagui', 'fuben', 'menpai') and not 1 <= len(targets) <= 2
            or activity == 'mijing' and not targets):
        raise ValueError('Invalid number of selected windows for '+activity)
    if len(set(targets)) != len(targets):
        raise ValueError('Select each window only once')
    if activity == 'mijing' and not identities:
        identities = [''] * len(targets)
    if (len(identities) != len(targets)
            or activity != 'mijing' and any(not identity.strip() for identity in identities)):
        raise ValueError('Provide one visible account/room identity per target')
    inventories = {}
    for path in inventory_paths:
        inventory = read_inventory(path)
        device = inventory['device']
        if device in inventories:
            raise ValueError('Duplicate inventory device: '+device)
        inventories[device] = inventory
    cfg = copy.deepcopy(template)
    defaults = cfg.get('windows', [])
    windows = []
    for index, (target, identity) in enumerate(zip(targets, identities)):
        device, sep, key = target.partition(':')
        if not sep or device not in inventories:
            raise ValueError('Unknown target: '+target)
        inventory = inventories[device]
        candidate = next((w for w in inventory['windows'] if w['id'] == key), None)
        if candidate is None:
            raise ValueError('Unknown target: '+target)
        require_uncovered(candidate, inventory)
        win = copy.deepcopy(defaults[index]) if index < len(defaults) else {}
        win.update(name='target-'+str(index+1), device=device,
                   rect=list(candidate['rect']), native_resolution=list(inventory['native_resolution']),
                   inventory_id=key, identity=identity.strip(), identity_text=identity.strip(),
                   role='account' if activity == 'mijing' else 'leader')
        if activity == 'jianhui':
            win['room_keyword'] = re.sub(r'的房间$', '', identity.strip())
        windows.append(win)
    cfg.update(windows=windows, calibrated=True,
               native_resolution=windows[0]['native_resolution'],
               window_selection=True,
               target_activity=activity)
    cfg.setdefault('device', windows[0]['device'])
    if activity == 'jianhui':
        cfg['device'] = 'pc1'  # the existing jianhui validator requires this default
    return cfg


def identity_matches(items, identity):
    normalize = lambda text: re.sub(r'\s+', '', text)
    expected = normalize(identity)
    return any(it['score'] >= .5 and normalize(it['text']) == expected for it in items)


def identity_box(win):
    box = win.get('identity_box', [0, 0, 1, .3])
    if (not isinstance(box, list) or len(box) != 4
            or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in box)
            or min(box[2:]) <= 0 or box[0]+box[2] > 1 or box[1]+box[3] > 1):
        raise ValueError('Invalid identity_box')
    return box


def resolve_accounts(cfg, inventories, images, recognize=region_ocr):
    """Match identities on each device, independently of previous positions."""
    resolved, used = [], set()
    cache = {}
    for win in cfg['windows']:
        device = win.get('device', cfg.get('device'))
        inventory, image = inventories[device], images[device]
        if image.size != tuple(inventory['native_resolution']):
            raise ValueError('Inventory/display resolution mismatch: '+device)
        identity = win.get('identity_text')
        if not identity and cfg.get('target_activity') == 'mijing':
            candidate = next((c for c in inventory['windows'] if c['id'] == win.get('inventory_id')), None)
            if candidate is None:
                raise ValueError('Selected account window disappeared: '+win['name'])
            if candidate['rect'] != win['rect']:
                raise ValueError('Anonymous account layout changed; re-select or provide identity: '+win['name'])
            matches = [candidate]
        elif not isinstance(identity, str) or not identity.strip():
            raise ValueError('Missing identity_text: '+win['name'])
        else:
            box = identity_box(win)
            matches = []
            for candidate in inventory['windows']:
                x, y, w, h = candidate['rect']
                key = (device, candidate['id'], tuple(box))
                if key not in cache:
                    cache[key] = recognize(image.crop((x, y, x+w, y+h)), box, 2)
                if identity_matches(cache[key], identity):
                    matches.append(candidate)
        if len(matches) != 1:
            raise ValueError(f'{device}: identity {identity!r} matched {len(matches)} windows; select a visible unique identity/identity_box')
        candidate = matches[0]
        require_uncovered(candidate, inventory)
        key = (device, candidate['id'])
        if key in used:
            raise ValueError('Two targets resolve to the same window: '+device)
        used.add(key)
        resolved.append(dict(win, rect=list(candidate['rect']),
                             native_resolution=list(inventory['native_resolution'])))
    return resolved


async def locate_targets(cfg, call, output, activity):
    if not cfg.get('window_selection'):
        return
    if cfg.get('target_activity') != activity:
        raise ValueError('Selected config belongs to another activity')
    devices = {w.get('device', cfg.get('device')) for w in cfg['windows']}
    inventories, images = {}, {}
    evidence = Path(output)/'startup'
    evidence.mkdir(exist_ok=True)
    for device in sorted(devices):
        content = await call('screenshot', device, format='png', max_width=2560,
                             wait_for_new_frame=True)
        note = ' '.join(c.text for c in content if c.type == 'text')
        dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
        if 'stale=False' not in note or dims is None:
            raise ValueError('No fresh native screenshot: '+device)
        blob = next((c.data for c in content if c.type == 'image'), None)
        if blob is None:
            raise ValueError('No screenshot: '+device)
        with Image.open(io.BytesIO(base64.b64decode(blob))) as image:
            images[device] = image.convert('RGB')
        images[device].save(evidence/(device+'.png'))
        if images[device].size != tuple(map(int, dims.groups())):
            raise ValueError('Screenshot was resized: '+device)
        inventories[device] = await asyncio.to_thread(discover_windows, images[device], device)
    windows = await asyncio.to_thread(resolve_accounts, cfg, inventories, images)
    cfg['windows'][:] = windows
    cfg['autocal'] = False  # do not remap verified accounts by nearest position
    (Path(output)/'selected-targets.json').write_text(
        json.dumps(windows, ensure_ascii=False, indent=2), encoding='utf-8')


@lru_cache(maxsize=1)
def layout_detector():
    # Reuse the existing screenshot-only jianhui layout tool without changing
    # its calibration path or importing activity code into the runtime loop.
    path = Path(__file__).resolve().parents[1]/'rustdesk-jianhui/scripts/auto_layout.py'
    spec = importlib.util.spec_from_file_location('mhxy_auto_layout', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.detect_windows


def discover_windows(image, device):
    try:
        rects, _ = layout_detector()(image, image.width, image.height, 1)
    except SystemExit as error:
        raise ValueError('Window discovery failed: '+str(error)) from error
    candidates = []
    for rect in sorted(rects, key=lambda r: (r[1], r[0])):
        x, y, w, h = rect
        if (min(x, y) < 0 or min(w, h) < 300 or x+w > image.width or y+h > image.height
                or not 1.15 <= w/h <= 1.55):
            raise ValueError('Window boundaries are ambiguous; tile unobscured 4:3 game windows')
        crop = image.crop((x, y, x+w, y+h))
        items = region_ocr(crop, [0, 0, 1, .15], 2)
        topbar = (text_hits(items, '活动', [0, 0, 1, .15], .5)
                  and any(text_hits(items, t, [0, 0, 1, .15], .5) for t in ('排行', '指引')))
        room = any('的房间' in it['text'] and it['score'] >= .5 for it in items)
        if not topbar and not room:
            raise ValueError('No independent game/room anchor in candidate '+str(rect))
        candidates.append(dict(id='w'+str(len(candidates)+1), rect=rect))
    if not candidates:
        raise ValueError('No visible game windows')
    return dict(device=device, native_resolution=list(image.size), windows=candidates)


def save_candidates(inventory, image, output):
    entries = []
    for candidate in inventory['windows']:
        x, y, w, h = candidate['rect']
        crop = image.crop((x, y, x+w, y+h))
        filename = candidate['id']+'.png'
        crop.save(output/filename)
        entries.append(dict(candidate, image=filename,
                            items=region_ocr(crop, [0, 0, 1, .3], 2)))
    (output/'windows.json').write_text(
        json.dumps(dict(inventory, windows=entries), ensure_ascii=False, indent=2), encoding='utf-8')


async def scan_devices(args):
    if __package__:
        from .session import load_spec, make_call, open_session
    else:
        from session import load_spec, make_call, open_session
    spec = load_spec(args.mcp_spec, args.repo.resolve())
    async with open_session(spec, args.repo.resolve(), {'RUSTDESK_SCREENSHOT_DIR': ''}) as session:
        call = make_call(session, lambda: False)
        for device in args.device:
            content = await call('screenshot', device, format='png', max_width=2560,
                                 wait_for_new_frame=True)
            note = ' '.join(c.text for c in content if c.type == 'text')
            dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
            if 'stale=False' not in note or dims is None:
                raise ValueError('No fresh screenshot: '+device)
            blob = next((c.data for c in content if c.type == 'image'), None)
            if blob is None:
                raise ValueError('No screenshot: '+device)
            with Image.open(io.BytesIO(base64.b64decode(blob))) as original:
                image = original.convert('RGB')
            if image.size != tuple(map(int, dims.groups())):
                raise ValueError('Screenshot was resized: '+device)
            output = args.output/device
            output.mkdir(parents=True, exist_ok=False)
            image.save(output/'desktop.png')
            inventory = await asyncio.to_thread(discover_windows, image, device)
            await asyncio.to_thread(save_candidates, inventory, image, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    scan = commands.add_parser('scan', help='Read-only RustDesk screenshots and local window discovery')
    scan.add_argument('--repo', type=Path, required=True)
    scan.add_argument('--device', action='append', required=True)
    scan.add_argument('--mcp-spec', type=Path)
    scan.add_argument('--output', type=Path, required=True)
    listing = commands.add_parser('list', help='List locally discovered candidates')
    listing.add_argument('--inventory', type=Path, action='append', required=True)
    inspect = commands.add_parser('inspect', help='Discover candidates from an offline native screenshot')
    inspect.add_argument('--device', required=True)
    inspect.add_argument('--image', type=Path, required=True)
    inspect.add_argument('--output', type=Path, required=True)
    select = commands.add_parser('select', help='Create runner config from selected leaders/accounts')
    select.add_argument('--inventory', type=Path, action='append', required=True)
    select.add_argument('--template', type=Path, required=True)
    select.add_argument('--activity', choices=ACTIVITIES, required=True)
    select.add_argument('--target', action='append', required=True, help='device:window-id, in plan order')
    select.add_argument('--identity', action='append', default=[], help='exact visible OCR text, in target order; optional for mijing')
    select.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'scan':
        for device in args.device:
            if not re.fullmatch(r'[a-z0-9_-]+', device):
                parser.error('Invalid device name')
        asyncio.run(scan_devices(args))
    elif args.command == 'list':
        print(json.dumps([read_inventory(p) for p in args.inventory], ensure_ascii=False, indent=2))
    elif args.command == 'inspect':
        if not re.fullmatch(r'[a-z0-9_-]+', args.device):
            parser.error('Invalid device name')
        with Image.open(args.image) as original:
            image = original.convert('RGB')
        inventory = discover_windows(image, args.device)
        args.output.mkdir(parents=True, exist_ok=False)
        save_candidates(inventory, image, args.output)
    else:
        cfg = build_config(json.loads(args.template.read_text()), args.inventory,
                           args.target, args.identity, args.activity)
        with args.output.open('x', encoding='utf-8') as output:
            json.dump(cfg, output, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
