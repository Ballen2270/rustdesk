"""Offline equality check and stationary-frame OCR timing, without MCP input."""
import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _framework.ocr_cache import RegionOCRCache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activity', choices=('fuben', 'zhuagui'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]/('rustdesk-'+args.activity)
    spec = importlib.util.spec_from_file_location(args.activity, root/'scripts'/f'{args.activity}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wf = module.build_workflow(dict(activity_list_scroll=[.65, .25, .22, .32]))
    cache = RegionOCRCache(module.region_ocr)
    rows = []
    for path in sorted((root/'references/screens').glob('*.png'), key=lambda p: int(p.stem)):
        original = Image.open(path).convert('RGB')
        image = original.crop((0, 25, original.width, original.height))
        baseline, baseline_items = module.recognize(image, wf)
        changed, changed_items = module.recognize(image, wf, cache=cache)
        hits, misses = cache.hits, cache.misses
        repeated, repeated_items = module.recognize(image, wf, cache=cache)
        def semantic(obs):
            return {k: v for k, v in obs.items() if k != 'recognition_seconds'}
        equal = (semantic(baseline) == semantic(changed) == semantic(repeated)
                 and baseline_items == changed_items == repeated_items)
        rows.append(dict(file=path.name, state=baseline['state'], equal=equal,
                         baseline_seconds=baseline['recognition_seconds'],
                         changed_seconds=changed['recognition_seconds'],
                         repeated_seconds=repeated['recognition_seconds'],
                         repeated_hits=cache.hits-hits, repeated_misses=cache.misses-misses))
    if not rows:
        raise RuntimeError('No reference images')
    report = dict(activity=args.activity, frames=len(rows), failures=[r['file'] for r in rows if not r['equal']],
                  median_baseline_seconds=statistics.median(r['baseline_seconds'] for r in rows),
                  median_changed_seconds=statistics.median(r['changed_seconds'] for r in rows),
                  median_repeated_seconds=statistics.median(r['repeated_seconds'] for r in rows), rows=rows)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != 'rows'}), flush=True)
    return bool(report['failures'])


if __name__ == '__main__':
    raise SystemExit(main())
