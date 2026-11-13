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
import os
import re
import time
from functools import lru_cache
from pathlib import Path

from PIL import Image

if __package__:
    from .vision import VisionUnavailable, region_ocr, text_hits
    from .window_geometry import measure_title_windows, measure_windows
else:
    from vision import VisionUnavailable, region_ocr, text_hits
    from window_geometry import measure_title_windows, measure_windows

ACTIVITIES = ('zhuagui', 'fuben', 'menpai', 'mijing', 'jianhui')

# Activities whose runner consumes a per-window occlusion map (masked OCR +
# click guards). They may select windows whose edges are overlapped by
# neighbor clients; every other activity keeps the strict uncovered rule.
OCCLUSION_AWARE_ACTIVITIES = {'menpai'}

# ---- per-device window inventory cache -----------------------------------
# Full-screen OCR discovery costs seconds per device (see FRAMEWORK.md
# "视觉吞吐与多设备规模"); game windows rarely move between runs. The cache
# only SHORTENS the search: it loads only when the pixel-measured window set
# of the CURRENT frame exactly matches the cached one (layout fingerprint,
# pure pixels, ±8 px per edge — a partial cache can never hit because the
# missing window's band is still measured on the frame) AND every cached rect
# still shows its 梦幻西游 title anchor. Any doubt or OCR error falls back to
# full discovery. A cache never authorizes input and never replaces the
# runners' own per-run alignment checks.
#
# RUSTDESK_WINDOW_CACHE: unset -> ~/.cache/rustdesk-mhxy; empty -> disabled.
CACHE_VERSION = 2
CACHE_FILE_SUFFIX = '.windows.json'
TITLE_BAND_PX = 64      # native pixels above a cached rect top: window chrome
MIN_TITLE_BAND_PX = 12  # windows flush with the screen top still show a sliver
FINGERPRINT_TOLERANCE_PX = 8  # per-edge tolerance of the layout fingerprint


def cache_root():
    raw = os.environ.get('RUSTDESK_WINDOW_CACHE')
    if raw is not None and not raw.strip():
        return None
    return (Path(raw).expanduser() if raw
            else Path.home()/'.cache'/'rustdesk-mhxy')


def cache_path(device):
    root = cache_root()
    return root/(device+CACHE_FILE_SUFFIX) if root else None


def _valid_cached_rect(rect, image):
    if (not isinstance(rect, list) or len(rect) != 4
            or any(type(v) is not int for v in rect)):
        return False
    x, y, w, h = rect
    return (min(x, y) >= 0 and min(w, h) >= 300 and y >= MIN_TITLE_BAND_PX
            and x+w <= image.width and y+h <= image.height
            and 1.15 <= w/h <= 1.55)


def _confirm_title_anchor(image, x, y, w):
    """3-variant title strip above a content rect: the exact strip and two
    padded crops. A single crop misses real titles for ±6 px rect jitter
    (measured 130/130 vs ~75% for one crop); each variant early-exits."""
    variants = ((max(0, y-40), y, 1),
                (max(0, y-40), y+4, 2),
                (max(0, y-52), y+16, 2))
    for top, bottom, scale in variants:
        band = image.crop((x, top, x+w, min(image.height, bottom)))
        if band.height <= 0:
            continue
        if any('梦幻西游' in it['text'] and it['score'] >= .3
               for it in region_ocr(band, [0, 0, 1, 1], scale)):
            return True
    return False


def load_cached_inventory(image, device):
    """Cached inventory when the CURRENT frame still shows exactly the cached
    layout (pixel fingerprint) and every title anchor, else None (the caller
    runs full discovery). Any OCR error is a cache miss, never a startup
    failure. The title band sits immediately ABOVE the cached rect:
    measure_windows derives a rect's top as the title bar's bottom edge."""
    path = cache_path(device)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get('version') != CACHE_VERSION
            or data.get('device') != device
            or data.get('native_resolution') != list(image.size)):
        return None
    entries = data.get('windows')
    if not isinstance(entries, list) or not entries:
        return None
    cached = []
    for entry in entries:
        rect = entry.get('rect') if isinstance(entry, dict) else None
        if not _valid_cached_rect(rect, image):
            return None
        cached.append(dict(id=str(entry.get('id') or 'w'+str(len(cached)+1)),
                           rect=list(rect)))
    # Layout fingerprint: every window measured on this frame must be a
    # cached rect and vice versa. Estimated recoveries never match (they are
    # never cached), so a startup taken while an overlay panel clips a window
    # cannot poison later runs with a partial inventory.
    measured = measure_windows(image) or []
    if len(measured) != len(cached):
        return None
    remaining = [entry['rect'] for entry in measured]
    for window in cached:
        x, y, w, h = window['rect']
        for index, rect in enumerate(remaining):
            if (abs(rect[0]-x) <= FINGERPRINT_TOLERANCE_PX
                    and abs(rect[1]-y) <= FINGERPRINT_TOLERANCE_PX
                    and abs(rect[2]-w) <= FINGERPRINT_TOLERANCE_PX
                    and abs(rect[3]-h) <= FINGERPRINT_TOLERANCE_PX):
                remaining.pop(index)
                break
        else:
            return None
    try:
        for window in cached:
            x, y, w, _ = window['rect']
            if not _confirm_title_anchor(image, x, y, w):
                return None
    except Exception:  # noqa: BLE001 — cache validation must never abort startup
        return None
    return dict(device=device, native_resolution=list(image.size), windows=cached)


def save_inventory_cache(device, inventory, image):
    """Best-effort: persist the freshly discovered rects for the next run.

    Only fully confirmed, fully measured windows are cached; a run whose
    layout carries an estimated edge or an unconfirmed window (Vision was
    degraded) writes nothing rather than freeze a doubtful layout."""
    path = cache_path(device)
    if path is None:
        return
    windows = []
    for win in inventory.get('windows') or []:
        if win.get('estimated') or win.get('unconfirmed'):
            continue
        rect = list(win.get('rect') or [])
        if not _valid_cached_rect(rect, image):
            continue
        windows.append(dict(id=win.get('id', ''), rect=rect))
    if not windows:
        return
    payload = dict(version=CACHE_VERSION, device=device,
                   native_resolution=list(image.size), windows=windows)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name+'.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
    except OSError:
        pass


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
        estimated = win.get('estimated', [])
        if (not isinstance(estimated, list)
                or any(edge not in ('right',) for edge in estimated)):
            raise ValueError('Inventory window has an unknown estimated edge')
    return data


def overlaps(a, b):
    return (max(a[0], b[0]) < min(a[0]+a[2], b[0]+b[2])
            and max(a[1], b[1]) < min(a[1]+a[3], b[1]+b[3]))


def minor_overlap(a, b):
    width = max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0]))
    height = max(0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
    return (width > 0 and height > 0
            and width*height <= .02*min(a[2]*a[3], b[2]*b[3])
            and (width <= .02*min(a[2], b[2])
                 or height <= .02*min(a[3], b[3])))


def require_uncovered(window, inventory):
    for other in inventory['windows']:
        if (other['id'] != window['id'] and overlaps(window['rect'], other['rect'])
                and not minor_overlap(window['rect'], other['rect'])):
            raise ValueError('Tile overlapping windows before selecting '+inventory['device']+':'+window['id'])


def covered_strips(rect, inventory, own_id=None):
    """Normalized strips of `rect` overlapped by other inventory windows.
    Z-order is unknown, so an overlap is conservatively treated as covered —
    clicks and OCR must stay out of it either way."""
    x, y, w, h = rect
    strips = []
    for other in inventory['windows']:
        if other['id'] == own_id:
            continue
        ox, oy, ow, oh = other['rect']
        left, top = max(x, ox), max(y, oy)
        right, bottom = min(x+w, ox+ow), min(y+h, oy+oh)
        if left < right and top < bottom:
            strips.append([(left-x)/w, (top-y)/h, (right-left)/w, (bottom-top)/h])
    return strips


def covered_fraction(box, strips):
    """Fraction of normalized `box` covered by the union of `strips`."""
    x0, y0, w, h = box
    if w <= 0 or h <= 0:
        return 0.0
    clips = []
    for sx, sy, sw, sh in strips:
        cx0, cy0 = max(x0, sx), max(y0, sy)
        cx1, cy1 = min(x0+w, sx+sw), min(y0+h, sy+sh)
        if cx0 < cx1 and cy0 < cy1:
            clips.append((cx0, cy0, cx1, cy1))
    if not clips:
        return 0.0
    edges = sorted({v for clip in clips for v in (clip[0], clip[2])})
    covered = 0.0
    for left, right in zip(edges, edges[1:]):
        spans = sorted((clip[1], clip[3]) for clip in clips
                       if clip[0] <= left and clip[2] >= right)
        merged, lo, hi = 0.0, None, None
        for slo, shi in spans:
            if hi is None or slo > hi:
                if hi is not None:
                    merged += hi-lo
                lo, hi = slo, shi
            else:
                hi = max(hi, shi)
        if hi is not None:
            merged += hi-lo
        covered += (right-left)*merged
    return min(1.0, covered/(w*h))


def ensure_selectable(window, inventory, occlusion_aware):
    """Occlusion-aware activities may pick windows whose edges are overlapped
    by neighbor clients (their runner masks and guards the strips); only a
    mostly hidden window is refused. Returns the window's covered strips."""
    strips = covered_strips(window['rect'], inventory, window.get('id'))
    if occlusion_aware:
        if covered_fraction([0, 0, 1, 1], strips) > .5:
            raise ValueError('Selected window is mostly hidden by neighbors; re-tile '
                             'before selecting '+inventory['device']+':'+window['id'])
    else:
        require_uncovered(window, inventory)
    return strips


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
        if candidate.get('estimated'):
            # A recovered-from-occlusion width is a peer-median prior: clicks
            # normalized to the rect could be off by the size difference.
            raise ValueError('Selected window '+target+' has an estimated '
                             'edge (overlay panel occlusion); rescan after '
                             'the connection panel hides')
        ensure_selectable(candidate, inventory, activity in OCCLUSION_AWARE_ACTIVITIES)
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
    aware = cfg.get('target_activity') in OCCLUSION_AWARE_ACTIVITIES
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
        if candidate.get('estimated'):
            raise ValueError('Matched window '+device+':'+candidate['id']+' has an '
                             'estimated edge (overlay panel occlusion); rescan after '
                             'the connection panel hides')
        strips = ensure_selectable(candidate, inventory, aware)
        key = (device, candidate['id'])
        if key in used:
            raise ValueError('Two targets resolve to the same window: '+device)
        used.add(key)
        entry = dict(win, rect=list(candidate['rect']),
                     native_resolution=list(inventory['native_resolution']))
        if aware:
            entry['occlusion'] = strips
        resolved.append(entry)
    return resolved


def _decode_native(blob, dims, device):
    with Image.open(io.BytesIO(base64.b64decode(blob))) as image:
        out = image.convert('RGB')
    if out.size != tuple(dims):
        raise ValueError('Screenshot was resized: '+device)
    return out


# ---- startup discovery ----------------------------------------------------
# Startup used to be one screenshot, one discovery, one identity pass; any
# transient (the host connection-manager panel on the first frame after a
# cold connect, a stale stream, one misread identity) aborted the run. These
# bound a retry loop around FRESH frames; see FRAMEWORK.md "启动与窗口识别".
STARTUP_ATTEMPTS = 3          # bounded fresh-frame attempts per run
STARTUP_RETRY_DELAY_S = 1.5   # between attempts (panel hide, stream settle)
STARTUP_DEADLINE_S = 90.0     # whole startup, identity retries included
ROW_CLUSTER_PX = 20           # rows closer than this are one row for wN ids


def order_row_major(rects):
    """Row-major ordering that survives 1-2 px row jitter: cluster by y
    (the cluster's first y anchors it), then sort each row by x. The plain
    (y, x) sort flipped wN ids whenever two rows differed by a pixel."""
    order = sorted(range(len(rects)), key=lambda i: (rects[i][1], rects[i][0]))
    rows, out = [], []
    for index in order:
        y = rects[index][1]
        if rows and abs(y-rows[-1][0]) <= ROW_CLUSTER_PX:
            rows[-1][1].append(index)
        else:
            rows.append([y, [index]])
    for _, indexes in rows:
        out.extend(sorted(indexes, key=lambda i: rects[i][0]))
    return out


def _title_anchor_count(image):
    """Number of 梦幻西游 title anchors on a full 1x frame — the
    completeness signal for failure diagnostics (more anchors than measured
    windows means a client was measured but not admitted)."""
    items = region_ocr(image, [0, 0, 1, 1], 1)
    return sum(1 for it in items
               if '梦幻西游' in it['text'] and it['score'] >= .3)


def _verify_candidate(image, rect, narrow=False):
    """OCR confirmation that a measured rect is a game client.

    2x first (Vision reads the small top-bar labels reliably only at 2x;
    1x hits ~40% of real windows), 1x fallback, then the title strip above
    the rect (battle/login frames hide the top bar but keep the title).
    A narrow (occlusion-recovered) band needs the title anchor itself: only
    it proves the clipped neutral bar belongs to a game client and not to
    an in-game dialog. Returns (ok, reason)."""
    x, y, w, h = rect
    if narrow:
        return _confirm_title_anchor(image, x, y, w), 'title'
    crop = image.crop((x, y, x+w, y+h))
    for scale in (2, 1):
        items = region_ocr(crop, [0, 0, 1, .15], scale)
        labels = sum(bool(text_hits(items, t, [0, 0, 1, .15], .25))
                     for t in ('活动', '排行', '指引'))
        if labels >= 2:
            return True, 'topbar@%dx' % scale
        if any('的房间' in it['text'] and it['score'] >= .5 for it in items):
            return True, 'room@%dx' % scale
    if _confirm_title_anchor(image, x, y, w):
        return True, 'title'
    return False, 'no topbar labels, room keyword or title anchor'


async def locate_targets(cfg, call, output, activity):
    if not cfg.get('window_selection'):
        return
    if cfg.get('target_activity') != activity:
        raise ValueError('Selected config belongs to another activity')
    devices = sorted({w.get('device', cfg.get('device')) for w in cfg['windows']})
    targets = {device: sum(1 for w in cfg['windows']
                           if w.get('device', cfg.get('device')) == device)
               for device in devices}
    evidence = Path(output)/'startup'
    evidence.mkdir(exist_ok=True)
    deadline = time.monotonic()+STARTUP_DEADLINE_S
    best = {}  # device -> (image, inventory); the attempt with the most
    # windows wins, fresher frames win ties (identity re-reads better on them)
    reports = {device: dict(device=device, attempts=[], cache=None,
                            configured_targets=targets[device])
               for device in devices}
    resolve_error = None

    async def scan(device, attempt):
        """One fresh frame for one device: cache fingerprint first, full
        discovery only when the cache cannot prove the layout is unchanged."""
        report = dict(attempt=attempt)
        reports[device]['attempts'].append(report)
        content = await call('screenshot', device, format='png', max_width=2560,
                             wait_for_new_frame=True)
        note = ' '.join(c.text for c in content if c.type == 'text')
        dims = re.search(r'native_resolution=(\d+)x(\d+)', note)
        blob = next((c.data for c in content if c.type == 'image'), None)
        if dims is None or blob is None:
            report['error'] = 'no native screenshot in tool response'
            return
        image = await asyncio.to_thread(
            _decode_native, blob, tuple(map(int, dims.groups())), device)
        # Every decoded frame is evidence, including the failed attempts.
        image.save(evidence/(device+'.png'))
        if attempt > 1:
            image.save(evidence/(device+'.attempt%d.png' % attempt))
        if 'stale=False' not in note:
            # Stream not ready (first frame after a cold connect, a dead
            # session, a display switch): retry on a later frame; a stale
            # frame never becomes the inventory.
            report['error'] = 'stale frame (no fresh decoded frame)'
            return
        inventory = await asyncio.to_thread(load_cached_inventory, image, device)
        if inventory is not None:
            reports[device]['cache'] = 'hit (fingerprint+title verified)'
            report['source'] = 'cache'
        else:
            inventory = await asyncio.to_thread(discover_windows, image, device)
            report['source'] = 'discovery'
        report['windows'] = [{k: w[k] for k in ('id', 'rect', 'estimated', 'unconfirmed')
                              if k in w} for w in inventory['windows']]
        report['rejected'] = inventory.get('rejected', [])
        if (device not in best
                or len(inventory['windows']) >= len(best[device][1]['windows'])):
            best[device] = (image, inventory)

    for attempt in range(1, STARTUP_ATTEMPTS+1):
        results = await asyncio.gather(*(scan(device, attempt) for device in devices),
                                       return_exceptions=True)
        for device, result in zip(devices, results):
            if isinstance(result, BaseException):
                reports[device]['attempts'][-1]['error'] = '%s: %s' % (
                    type(result).__name__, result)
        if all(device in best for device in devices):
            images = {device: image for device, (image, _) in best.items()}
            inventories = {device: inv for device, (_, inv) in best.items()}
            try:
                windows = await asyncio.to_thread(resolve_accounts, cfg,
                                                  inventories, images)
            except (ValueError, VisionUnavailable) as exc:
                # Identity matching is one exact-string OCR pass; one misread
                # or an identity band occluded by the startup panel aborts
                # it. A fresh frame retries both the read and the layout.
                resolve_error = exc
                for report in reports.values():
                    report['resolve_error'] = str(exc)
            else:
                cfg['windows'][:] = windows
                cfg['autocal'] = False  # do not remap verified accounts by nearest position
                (Path(output)/'selected-targets.json').write_text(
                    json.dumps(windows, ensure_ascii=False, indent=2), encoding='utf-8')
                # Cache only after this run's targets resolved on the layout.
                for device in devices:
                    await asyncio.to_thread(save_inventory_cache, device,
                                            inventories[device], images[device])
                _write_discovery_reports(evidence, reports)
                return
        if attempt < STARTUP_ATTEMPTS and time.monotonic()+STARTUP_RETRY_DELAY_S < deadline:
            await asyncio.sleep(STARTUP_RETRY_DELAY_S)
        else:
            break

    # Every attempt failed: add the title-anchor completeness signal and
    # raise one aggregated, per-device actionable error (single-device
    # failures used to hide behind the first exception).
    for device, (image, _) in best.items():
        try:
            reports[device]['anchor_count'] = await asyncio.to_thread(
                _title_anchor_count, image)
        except Exception:  # noqa: BLE001 — diagnostics only
            reports[device]['anchor_count'] = 'unavailable'
    _write_discovery_reports(evidence, reports)
    details = []
    for device in devices:
        report = reports[device]
        found = ([w['id'] for w in best[device][1]['windows']]
                 if device in best else [])
        errors = '; '.join(filter(None, (a.get('error') for a in report['attempts'])))
        details.append(
            '%s: %d attempt(s), found %d window(s) %s, title anchors %s, '
            'targets %d%s, evidence %s'
            % (device, len(report['attempts']), len(found), found,
               report.get('anchor_count'), report['configured_targets'],
               ' — '+str(resolve_error) if resolve_error else '',
               evidence/(device+'.discovery.json')))
    raise ValueError('Startup window discovery failed; no fresh confirmed '
                     'layout: '+' | '.join(details))


def _write_discovery_reports(evidence, reports):
    """Per-device startup diagnosis: attempts, what was found, what was
    rejected and why, cache provenance, resolve errors."""
    for device, report in reports.items():
        try:
            (evidence/(device+'.discovery.json')).write_text(
                json.dumps(report, ensure_ascii=False, indent=1, default=str),
                encoding='utf-8')
        except OSError:
            pass


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
    measured_entries = measure_windows(image)
    if not measured_entries:
        # No plausible pixel bands (no neutral title bars on this frame):
        # fall back to the OCR-driven legacy detectors.
        measured = None
        try:
            rects, items = layout_detector()(image, image.width, image.height, 1)
        except SystemExit as error:
            # Title anchors are large glyphs: try the full-screen pass at 1x
            # first (a quarter of the 2x pixel count); 2x is the fallback for
            # narrow windows, not the default.
            items = region_ocr(image, [0, 0, 1, 1], 1)
            measured = measure_title_windows(image, items)
            if measured is None:
                items = region_ocr(image, [0, 0, 1, 1], 2)
                measured = measure_title_windows(image, items)
            if measured is None:
                raise ValueError('Window discovery failed: '+str(error)) from error
            rects = measured
        candidates_native = (measure_title_windows(image, items)
                             if items and measured is None else None)
        if (any(min(r[2:]) < 300 or not 1.15 <= r[2]/r[3] <= 1.55 for r in rects)
                or candidates_native is not None and sorted(candidates_native) != sorted(rects)):
            if items:
                more = region_ocr(image, [0, 0, 1, 1], 1)
                measured = measure_title_windows(image, items+more)
                if measured is None:
                    more = region_ocr(image, [0, 0, 1, 1], 2)
                    measured = measure_title_windows(image, items+more)
            else:
                measured = measure_title_windows(image, items)
            if measured is not None:
                rects = measured
        measured_entries = [dict(rect=list(rect), estimated=[], narrow=False)
                            for rect in rects]
    candidates, rejected = [], []
    for index in order_row_major([entry['rect'] for entry in measured_entries]):
        entry = measured_entries[index]
        x, y, w, h = entry['rect']
        left, top = max(0, x), max(0, y)
        right, bottom = min(image.width, x+w), min(image.height, y+h)
        if right-left < 300 or bottom-top < 300:
            rejected.append((entry['rect'], 'clipped below 300px'))
            continue
        rect = [left, top, right-left, bottom-top]
        x, y, w, h = rect
        if not 1.15 <= w/h <= 1.55:
            rejected.append((rect, 'not a 4:3-ish content rect'))
            continue
        try:
            ok, reason = _verify_candidate(image, rect, entry['narrow'])
        except VisionUnavailable:
            if entry['narrow']:
                # A recovered band is a peer-median prior until its own
                # title confirms it; without OCR it stays out (fail closed).
                rejected.append((rect, 'narrow band; vision unavailable'))
                continue
            # Pixel geometry stands; mark the window so it is never cached
            # and downstream can see the confirmation did not run.
            candidates.append(dict(id='w'+str(len(candidates)+1), rect=rect,
                                   estimated=list(entry['estimated']),
                                   unconfirmed=True))
            continue
        if not ok:
            rejected.append((rect, reason))
            continue
        candidates.append(dict(id='w'+str(len(candidates)+1), rect=rect,
                               estimated=list(entry['estimated'])))
    if not candidates:
        raise ValueError('No visible game windows')
    return dict(device=device, native_resolution=list(image.size),
                windows=candidates, rejected=rejected)


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
