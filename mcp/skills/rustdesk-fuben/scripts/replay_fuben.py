#!/usr/bin/env python3
"""Replay the reference frames through the live recognition pipeline.

Verifies per frame: expected state and a resolvable click target for every
workflow node the state exposes. Three size variants per frame."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys

from PIL import Image, ImageDraw
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _framework.workflow import ClickPoint
from fuben import (FubenWorkflow, build_screens, build_workflow, detect_ready_popups,
                   dialog_option, enter_button, recognize, task_row, unique_text,
                   wildcard_row)

EXPECTED = {1: 'normal', 2: 'activity_panel', 3: 'baixiao_menu', 4: 'dungeon_panel',
            5: 'dungeon_panel', 6: 'knight_wait', 7: 'knight_wait', 8: 'dungeon_map',
            9: 'dungeon_map', 10: 'npc_dialog', 11: 'battle', 12: 'dungeon_map',
            13: 'dungeon_map', 14: 'npc_dialog', 15: 'unknown', 16: 'dungeon_map',
            17: 'dungeon_map', 18: 'npc_dialog', 19: 'dungeon_map', 20: 'reward',
            21: 'dungeon_done', 22: 'normal', 23: 'activity_panel', 24: 'dungeon_panel',
            26: 'dungeon_panel', 15: frozenset(('unknown', 'battle'))}


def target_for(node, items):
    if node.id == 'join_dungeon':
        return wildcard_row(items)[1]
    if node.id == 'advance_task':
        return task_row(items)
    if node.id == 'dialog_confirm':
        # Geometric click: the option band always resolves; a readable item
        # is only the better anchor.
        return (dialog_option(items) or
                dict(text='(fixed box)', score=1.0,
                     **dict(zip(('x', 'y', 'w', 'h'), node.act.box))))
    if node.id == 'enter_dungeon':
        return enter_button(items)
    if isinstance(node.act, ClickPoint):
        return dict(text='(fixed box)', score=1.0,
                    **dict(zip(('x', 'y', 'w', 'h'), node.act.box)))
    return unique_text(items, node.act.text, node.act.box)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', type=Path,
                        default=Path(__file__).resolve().parents[1]/'references/screens')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    wf = build_workflow(dict(activity_list_scroll=[.65, .25, .22, .32]))
    rows = []
    for number, expected in EXPECTED.items():
        path = args.images/f'{number}.png'
        original = Image.open(path).convert('RGB')
        # The supplied references each have a 25px Windows title bar.
        image = original.crop((0, 25, original.width, original.height))
        content = image
        for variant, size in [('native', content.size), ('small', (760, 570)),
                              ('desktop', (957, 719)), ('four', (676, 490))]:
            image = content.resize(size, Image.Resampling.LANCZOS)
            obs, items = recognize(image, wf)
            actions = []
            for node in wf.nodes_for(obs['state']):
                # The skip button only exists on story beats; frames without
                # one legitimately expose no skip target.
                if node.id == 'skip_story' and not any('跳过剧情' in it['text'] for it in items):
                    continue
                actions.append(dict(action=node.id, target=target_for(node, items)))
            wanted = expected if isinstance(expected, frozenset) else {expected}
            ok = obs['state'] in wanted and all(a['target'] is not None for a in actions)
            row = dict(file=path.name, variant=variant,
                       sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                       recognition_size=list(image.size),
                       expected=sorted(wanted), observation=obs,
                       actions=actions, ok=ok)
            rows.append(row)
            (args.output/f'{number}-{variant}-ocr.json').write_text(
                json.dumps(dict(**row, items=items), ensure_ascii=False, indent=2))
            annotated = image.copy()
            drawing = ImageDraw.Draw(annotated)
            for action in actions:
                if action['target']:
                    it = action['target']
                    drawing.rectangle((it['x']*image.width, it['y']*image.height,
                                       (it['x']+it['w'])*image.width,
                                       (it['y']+it['h'])*image.height),
                                      outline=(255, 40, 40), width=2)
            annotated.save(args.output/f'{number}-{variant}-targets.png')
    # ---- member ready-popup detection at the live window widths ----
    # The sweep pairs a 准? read with a same-row 拒绝 on the full device
    # frame; 960 matches the pc1d1 members, 850/680/615 span the pc2/pc3
    # three-size tiling measured live (615 is the smallest — the button
    # OCRs as 准留 there and only the zoomed cascade rescues it).
    def member_canvas(pastes):
        canvas = Image.new('RGB', (1920, 1080), (28, 34, 40))
        for src_name, x0, y0, width in pastes:
            src = Image.open(args.images/src_name).convert('RGB')
            src = src.crop((0, 25, src.width, src.height))   # drop title bar
            src = src.resize((width, round(src.height*width/src.width)),
                             Image.Resampling.LANCZOS)
            canvas.paste(src, (x0, y0))
        return canvas
    member_cases = [
        ('7-member-960', [('7.png', 0, 0, 960)], 1),
        ('7-member-850', [('7.png', 0, 0, 850)], 1),
        ('7-member-680', [('7.png', 0, 0, 680)], 1),
        ('7-member-615', [('7.png', 0, 0, 615)], 1),
        ('7-member-two', [('7.png', 0, 0, 960), ('7.png', 1000, 200, 615)], 2),
        ('1-member-city', [('1.png', 0, 0, 960)], 0),
    ]
    for name, pastes, want in member_cases:
        canvas = member_canvas(pastes)
        buttons, items = detect_ready_popups(canvas)
        row = dict(file=name, variant='member',
                   sha256=hashlib.sha256(canvas.tobytes()).hexdigest(),
                   recognition_size=list(canvas.size),
                   expected=[f'{want} popup(s)'],
                   observation=dict(state='member_ready', source='full_ocr',
                                    recognition_seconds=0.0),
                   actions=[dict(action='member_ready', target=b) for b in buttons],
                   ok=len(buttons) == want)
        rows.append(row)
        (args.output/f'{name}-ocr.json').write_text(
            json.dumps(dict(**row, items=items), ensure_ascii=False, indent=2))
        annotated = canvas.copy()
        drawing = ImageDraw.Draw(annotated)
        for a in row['actions']:
            b = a['target']
            drawing.rectangle((b['x']*canvas.width, b['y']*canvas.height,
                               (b['x']+b['w'])*canvas.width,
                               (b['y']+b['h'])*canvas.height),
                              outline=(255, 40, 40), width=2)
        annotated.save(args.output/f'{name}-targets.png')
    failures = [r for r in rows if not r['ok']]
    print(json.dumps(dict(median_seconds=statistics.median(
        r['observation']['recognition_seconds'] for r in rows),
        failures=[f"{r['file']}:{r['variant']}→{r['observation']['state']}" for r in failures]),
        ensure_ascii=False))


if __name__ == '__main__':
    raise SystemExit(main())
