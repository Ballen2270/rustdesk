#!/usr/bin/env python3
"""Offline screenshot experiment. No device calls, input, or inventory cache.

conda run -n rustdesk-mcp python mcp/experiments/window_detection/verify.py \
    --images /tmp/wintest-095838 --output /tmp/window-verification
"""
from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import re
import statistics
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

FRAMEWORK = Path(__file__).resolve().parents[2]/'skills'/'_framework'
sys.path.insert(0, str(FRAMEWORK))
from vision import region_ocr  # noqa: E402
from window_geometry import _title_bands  # noqa: E402
from window_targets import discover_windows  # noqa: E402


def cpu_ocr(image):
    import Quartz
    import Vision
    from vision import _vision_perform

    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    raw = buffer.getvalue()
    data = Quartz.CFDataCreate(None, raw, len(raw))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    if source is None:
        raise RuntimeError('Cannot decode OCR image')
    cg = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)

    def perform():
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setUsesCPUOnly_(True)
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(['zh-Hans', 'en-US'])
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(str(error))
        return request

    result = []
    for observation in _vision_perform(perform).results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        box = observation.boundingBox()
        result.append(dict(text=str(candidate.string()).replace(' ', ''),
                           score=float(candidate.confidence()), x=float(box.origin.x),
                           y=float(1-box.origin.y-box.size.height),
                           w=float(box.size.width), h=float(box.size.height)))
    return result


def merged_bands(image):
    bands = []
    for band in sorted(_title_bands(image), key=lambda b: (b['y0'], b['x0'])):
        for previous in bands:
            if (abs(previous['x0']-band['x0']) <= 6
                    and abs(previous['x1']-band['x1']) <= 6
                    and 0 <= band['y0']-previous['y1'] <= 18
                    and band['y1']-previous['y0'] <= 42):
                previous['y1'] = band['y1']
                break
        else:
            bands.append(dict(band))
    return bands


def scan_ocr(image, box, evidence, label, scale=2):
    crop = image.crop(box)
    items = region_ocr(crop, [0, 0, 1, 1], scale)
    evidence.append(dict(region=label, box=list(box), scale=scale, items=items))
    return items


def border_candidates(image, left, top, width):
    pixels = image.load()
    columns = range(left+5, min(image.width-1, left+width-5), 4)
    edges = []
    for y in range(top+round(width*.68), min(image.height, top+round(width*.82))):
        differences = [sum(abs(pixels[x, y][c]-pixels[x, y-1][c])
                           for c in range(3))/3 for x in columns]
        if not differences:
            continue
        coverage = sum(d > 20 for d in differences)/len(differences)
        if coverage >= .7 and statistics.mean(differences) >= 25:
            edges.append(dict(y=y, coverage=round(coverage, 3)))
    return edges


def hud_bars(image, box):
    crop = image.crop(box)
    pixels = crop.load()
    runs = {'orange': [], 'blue': []}
    for y in range(crop.height):
        for name in runs:
            start = None
            for x in range(crop.width+1):
                hit = False
                if x < crop.width:
                    r, g, b = pixels[x, y]
                    hit = (r > 150 and 70 < g < 210 and b < 110 if name == 'orange'
                           else b > 130 and b > r*1.2 and g > 80)
                if hit and start is None:
                    start = x
                elif not hit and start is not None:
                    if x-start >= 15:
                        runs[name].append((start, x, y))
                    start = None
    return any(2 <= by-oy <= 18 and min(oright, bright)-max(oleft, bleft) >= 12
               for oleft, oright, oy in runs['orange']
               for bleft, bright, by in runs['blue'])


def prototype(image):
    evidence, candidates, rejected = [], [], []
    for band in merged_bands(image):
        x, top = band['x0'], band['y1']+1
        title_hit = False
        for scale, right in ((2, band['x1']), (1, min(band['x1'], x+240)),
                             (2, min(band['x1'], x+240))):
            title = scan_ocr(image, (x, max(0, band['y0']-4), right,
                                     min(image.height, top+4)), evidence, 'title', scale)
            title_hit = any('梦幻西游' in item['text'] and item['score'] >= .25
                            for item in title)
            if title_hit:
                break
        if not title_hit:
            rejected.append(dict(band=band, reason='no_game_title'))
            continue
        for scale in (2, 1, 3):
            map_items = scan_ocr(image, (x, top, min(image.width, x+145),
                                        min(image.height, top+65)), evidence, 'map', scale)
            coordinate = any(re.search(r'[（(]\s*\d+\s*[,，、.:：]\s*\d+', it['text'])
                             for it in map_items)
            clock = any(re.search(r'\d{1,2}[:：]\d{2}', it['text'])
                        or '时' in it['text'] for it in map_items)
            if coordinate and clock:
                break
        if not coordinate or not clock:
            rejected.append(dict(band=band, reason='map_not_verified'))
            continue
        candidates.append(dict(band=band, title_verified=True, map_verified=True))

    widths = [c['band']['x1']-c['band']['x0'] for c in candidates]
    typical = statistics.median(widths) if widths else 0
    windows = []
    for candidate in candidates:
        band = candidate['band']
        x, top = band['x0'], band['y1']+1
        visible_width = band['x1']-x
        truncated = visible_width < typical*.8
        width = round(typical) if truncated else visible_width
        width = min(width, image.width-x)
        edges = border_candidates(image, x, top, width)
        bottom = min(edges, key=lambda e: abs(e['y']-(top+width*.75))) if edges else None
        # A peer-width estimate is not a measured right boundary or permission to click.
        rect = [x, top, width, bottom['y']-top if bottom else round(width*.75)]
        hud_box = (max(x, x+width-155), top, x+width, min(image.height, top+65))
        windows.append(dict(
            rect=rect, title_box=[x, band['y0'], visible_width, top-band['y0']],
            title_verified=True, map_verified=True,
            avatar_hud_bar_support=hud_bars(image, hud_box),
            geometry_status='estimated' if truncated or bottom is None else 'measured',
            right_boundary_source='peer_width_prior' if truncated else 'title_band',
            bottom_candidates=edges, input_authorized=False))
    return dict(windows=windows, rejected=rejected, ocr=evidence)


def evaluate(windows, expected, tolerance):
    if len(windows) != len(expected):
        return dict(count_ok=False, localization_ok=False, geometry_ok=False,
                    expected_count=len(expected), actual_count=len(windows))
    if not expected:
        return dict(count_ok=True, localization_ok=True, geometry_ok=True, matches=[])
    # Match by origins only; the detector never receives the ground-truth rectangles.
    order = min(itertools.permutations(range(len(windows))), key=lambda permutation:
                sum(abs(windows[j]['rect'][0]-r[0])+abs(windows[j]['rect'][1]-r[1])
                    for j, r in zip(permutation, expected)))
    matches = []
    for index, truth in zip(order, expected):
        actual = windows[index]['rect']
        origin_error = max(abs(actual[k]-truth[k]) for k in (0, 1))
        edge_error = max(origin_error, abs(actual[0]+actual[2]-truth[0]-truth[2]),
                         abs(actual[1]+actual[3]-truth[1]-truth[3]))
        matches.append(dict(expected=truth, actual=actual, origin_error_px=origin_error,
                            max_edge_error_px=edge_error,
                            geometry_status=windows[index].get('geometry_status', 'baseline')))
    return dict(count_ok=True,
                localization_ok=all(m['origin_error_px'] <= tolerance for m in matches),
                geometry_ok=all(m['max_edge_error_px'] <= tolerance for m in matches),
                matches=matches)


def overlay(image, result, expected, path):
    out = image.copy()
    draw = ImageDraw.Draw(out)
    for x, y, w, h in expected:
        draw.rectangle((x, y, x+w-1, y+h-1), outline='#00e5ff', width=2)
    for number, win in enumerate(result.get('windows', []), 1):
        x, y, w, h = win['rect']
        color = '#ff9f00' if win.get('geometry_status') == 'estimated' else '#ff00ff'
        draw.rectangle((x, y, x+w-1, y+h-1), outline=color, width=3)
        draw.text((x+8, y+75), f"{number}: {win.get('geometry_status', 'baseline')}",
                  fill='white', stroke_width=2, stroke_fill='black')
    out.save(path)


def run_detector(detector, image):
    start = time.perf_counter()
    try:
        result = detector(image)
    except (Exception, SystemExit) as error:
        result = dict(windows=[], error=f'{type(error).__name__}: {error}')
    result['seconds'] = round(time.perf_counter()-start, 3)
    return result


def variants(image, expected, extended):
    yield 'original', image, expected
    if not extended:
        return
    shifted = Image.new('RGB', image.size, '#303030')
    shifted.paste(image, (4, 3))
    yield 'shift-4-3', shifted, [[x+4, y+3, w, h] for x, y, w, h in expected]
    size = (round(image.width*.97), round(image.height*.97))
    yield 'scale-97pct', image.resize(size, Image.Resampling.LANCZOS), [
        [round(v*.97) for v in r] for r in expected]
    yield 'blank-negative', Image.new('RGB', image.size, '#dddddd'), []
    titles = Image.new('RGB', image.size, '#303030')
    for x, y, w, _ in expected:
        box = (x, max(0, y-32), x+w, y)
        titles.paste(image.crop(box), box[:2])
    yield 'titles-only-negative', titles, []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('cases.json'))
    parser.add_argument('--extended', action='store_true')
    parser.add_argument('--skip-baseline', action='store_true')
    parser.add_argument('--cpu-ocr', action='store_true',
                        help='Use CPU-only Apple Vision for both detectors in this process')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    sources = []
    for case in manifest['cases']:
        path = args.images/case['file']
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != case['sha256']:
            parser.error(f'Source hash mismatch: {path}; use an independently annotated manifest')
        with Image.open(path) as im:
            image = im.convert('RGB')
        if list(image.size) != case['size']:
            parser.error(f'Source dimensions mismatch: {path}')
        sources.append((case, image))
    args.output.mkdir(parents=True, exist_ok=False)
    if args.cpu_ocr:
        import vision
        vision.ocr = cpu_ocr
    report = dict(scope='offline detection only; no identity or input authorization',
                  ocr_backend='Apple Vision CPU' if args.cpu_ocr else 'Apple Vision automatic',
                  annotation_note=manifest['annotation_note'], cases=[])
    passed = True
    for case, image in sources:
        for variant, frame, truth in variants(image, case['rects'], args.extended):
            key = case['id']+'-'+variant
            entry = dict(id=key, source=case['file'], sha256=case['sha256'], expected=truth)
            detectors = {'prototype': prototype}
            if not args.skip_baseline:
                detectors['baseline'] = lambda im: discover_windows(im, 'offline')
            for name, detector in detectors.items():
                result = run_detector(detector, frame)
                result['evaluation'] = evaluate(result['windows'], truth, manifest['tolerance_px'])
                overlay(frame, result, truth, args.output/(key+'-'+name+'.png'))
                entry[name] = result
                check = result['evaluation']
                if name == 'prototype':
                    passed &= not result.get('error') and check['localization_ok'] and check['geometry_ok']
                print(key, name, 'windows=', len(result['windows']),
                      'localization=', check['localization_ok'], 'geometry=', check['geometry_ok'],
                      'seconds=', result['seconds'], result.get('error', ''), flush=True)
            report['cases'].append(entry)
            (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    report['prototype_checks_passed'] = bool(passed)
    (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
