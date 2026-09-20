"""Run-directory artifact helpers shared by skill runners.

Artifact protocol (viewer-compatible): state.json rewritten atomically,
events.jsonl / timeline.jsonl append-only, evidence shots named
``{ns}-{win}-{tag}.png``. Never rename or remove these — the web console
depends on them.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def save(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def shot_name(win, tag):
    return f"{time.time_ns()}-{win}-{tag}.png"


class JsonlLogger:
    """Append-only JSONL writer (events.jsonl / timeline.jsonl)."""

    def __init__(self, path):
        self.path = Path(path)

    def write(self, entry):
        with self.path.open('a') as f:
            f.write(json.dumps(entry, ensure_ascii=False)+'\n')


def event_logger(run_dir):
    log = JsonlLogger(Path(run_dir)/'events.jsonl')

    def event(**entry):
        log.write(dict(time=time.time(), **entry))
    return event
