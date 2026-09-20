#!/usr/bin/env python3
"""Offline reference replay for mijing: expected screen per reference image.

Measures local recognition only; never connects to a device. The battle
reference comes from the jianhui image set (same battle engine UI).
"""
import argparse
import json
import statistics
from pathlib import Path

from PIL import Image

from mijing import MijingWorkflow, build_screens, recognize

EXPECTED = {
    '游戏正常界面.png': 'normal',
    '活动面板.png': 'activity_panel',
    '云乐游-秘境降妖.png': 'yunleyou_menu',
    '秘境降妖面板.png': 'mijing_panel',
    '进入秘境降妖.png': 'in_instance',
    '秘境降妖18关.png': 'fight_menu',
    '秘境降妖24关.png': 'fight_menu',
    '秘境25关.png': 'fight_menu',
    '通关.png': 'cleared',
    '战斗ing.png': 'battle',
}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--images', type=Path, required=True)
    args = p.parse_args()
    wf = MijingWorkflow(build_screens(), [])
    times, failures, rows = [], [], []
    for name, expected in EXPECTED.items():
        path = args.images/'normal'/name
        if not path.exists():
            path = args.images/name  # shared references (battle) sit at the root
        with Image.open(path) as im:
            obs, items = recognize(im.convert('RGB'), wf)
        times.append(obs['recognition_seconds'])
        level = obs.get('level')
        ok = obs['state'] == expected
        if not ok:
            failures.append(name)
        rows.append(dict(file=name, expected=expected, state=obs['state'], level=level,
                         source=obs['source'], seconds=obs['recognition_seconds']))
        print(json.dumps(rows[-1], ensure_ascii=False))
    print(json.dumps(dict(median_seconds=statistics.median(times), max_seconds=max(times),
                          failures=failures), ensure_ascii=False))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
