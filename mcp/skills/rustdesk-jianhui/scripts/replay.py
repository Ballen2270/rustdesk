"""Offline reference replay. Measures local recognition only; never connects to pc1."""
import argparse
import json
import statistics
from pathlib import Path

from PIL import Image
from jianhui import recognize
from local_features import BattleAnchors


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--images', type=Path, required=True)
    args = p.parse_args()
    expected = {'匹配界面单个.png': 'room', '匹配中.png': 'matching',
                '结算完成界面.png': 'panel', '战斗ing.png': 'battle',
                '战斗失败.png': 'result', '匹配成功.png': 'unknown'}
    times, failures = [], []
    anchors = BattleAnchors()
    for name, state in expected.items():
        with Image.open(args.images/name) as im:
            obs, _ = recognize(im.convert('RGB'))
            if name == '战斗ing.png':
                anchors.learn(im)
            if obs['state'] != state:
                failures.append(name)
            if name == '匹配中.png' and obs['ready'] != [True]*4:
                failures.append('missed ready slot')
            if name == '匹配界面单个.png' and obs['ready'] != [True, False, False, True]:
                failures.append('incorrect ready slots')
            times.append(obs['recognition_seconds'])
            print(json.dumps(dict(file=name, expected=state, **obs), ensure_ascii=False), flush=True)
    with Image.open(args.images/'战斗ing.png') as im:
        if not anchors.matches(im):
            failures.append('battle anchors self-match')
    # Resize negatives to the same dimensions so this tests pixels, not merely size checks.
    for name in expected:
        if name == '战斗ing.png':
            continue
        with Image.open(args.images/name) as im:
            if anchors.matches(im.resize(anchors.size)):
                failures.append('false battle anchor match: '+name)
    print(json.dumps(dict(median_seconds=statistics.median(times), max_seconds=max(times), failures=failures)))
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
