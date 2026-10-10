#!/usr/bin/env python3
"""Offline replay through the real recognition cascade; no MCP or device connection."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from PIL import Image, ImageDraw
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _framework.workflow import ClickPoint
from zhuagui import (ZhuaguiWorkflow, build_screens, build_workflow, recognize,
                     row_button, unique_text)

EXPECTED = {1: 'activity_panel', 2: 'claim_menu', 3: 'task', 4: 'task',
            5: 'battle', 6: 'round_done', 7: 'claim_menu', 8: 'task', 9: 'task'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--images', type=Path, default=Path(__file__).resolve().parents[1]/'references/screens')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    wf = build_workflow(dict(activity_list_scroll=[.65, .25, .22, .32]))
    rows = []
    for number, expected in EXPECTED.items():
        path = args.images/f'{number}.png'
        original = Image.open(path).convert('RGB')
        # The supplied references each have a 25px Windows title bar.
        image = original.crop((0, 25, original.width, original.height))
        content = image
        for variant, size in [('native', content.size), ('small', (760, 570)), ('desktop', (957, 719))]:
            image = content.resize(size, Image.Resampling.LANCZOS)
            obs, items = recognize(image, wf)
            actions = []
            for node in wf.nodes_for(obs['state']):
                if isinstance(node.act, ClickPoint):
                    target = dict(text='(fixed scene tap)', score=1.0,
                                  **dict(zip(('x', 'y', 'w', 'h'), node.act.box)))
                else:
                    target = row_button(items)[1] if node.id == 'join_entry' else unique_text(items, node.act.text, node.act.box)
                actions.append(dict(action=node.id, target=target))
            ok = obs['state'] == expected and all(a['target'] is not None for a in actions)
            row = dict(file=path.name, variant=variant, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                       content_rect=[0, 25, content.width, content.height], recognition_size=list(image.size), expected=expected,
                       observation=obs, actions=actions, ok=ok)
            rows.append(row)
            (args.output/f'{number}-{variant}-ocr.json').write_text(json.dumps(dict(**row, items=items), ensure_ascii=False, indent=2))
            annotated = image.copy()
            drawing = ImageDraw.Draw(annotated)
            for action in actions:
                if action['target']:
                    it = action['target']
                    drawing.rectangle((it['x']*image.width, it['y']*image.height,
                                       (it['x']+it['w'])*image.width, (it['y']+it['h'])*image.height),
                                      outline='red', width=3)
            annotated.save(args.output/f'{number}-{variant}-targets.png')
            print(json.dumps(row, ensure_ascii=False), flush=True)
    report = dict(rows=rows, median_seconds=statistics.median(r['observation']['recognition_seconds'] for r in rows),
                  failures=[r['file']+':'+r['variant'] for r in rows if not r['ok']])
    (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k != 'rows'}))
    return bool(report['failures'])


if __name__ == '__main__':
    raise SystemExit(main())
