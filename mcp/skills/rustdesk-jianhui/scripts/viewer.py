#!/usr/bin/env python3
"""Read-only web viewer for a jianhui run directory (live or finished).

Serves the run's state/events/timeline artifacts and images over HTTP; the
only write operation is creating the STOP file, which is exactly the
documented manual stop. Never talks to the bridge or the device. Default
bind is loopback; pass --host 0.0.0.0 to opt into LAN viewing.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TAIL = 500
IMAGE_CAP = 400
SAFE_PART = re.compile(r'^[\w\-.]+$')
SUFFIXES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}


def read_jsonl(path, after=0):
    """Return (parsed entries after line `after`, total line count)."""
    if not path.exists():
        return [], 0
    entries, total = [], 0
    with path.open() as f:
        for line in f:
            total += 1
            if total <= after:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # torn tail line while the runner appends
            if len(entries) >= TAIL:
                break
    return entries, total


class Viewer:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir

    # ---- garbage collection ------------------------------------------------
    # Policy lives in <run>/GC.json, written by the UI. No file (or
    # enabled=false) means: never delete anything — opening an old run is
    # safe by default. Protected forever: latest-*.png and the action
    # before/after pairs; everything else graphical ages out.

    def policy(self):
        p = self.run_dir/'GC.json'
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        return None

    def set_policy(self, policy):
        if not isinstance(policy, dict):
            raise ValueError('policy must be an object')
        enabled = policy.get('enabled')
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be a boolean')
        ages = {}
        for key in ('frames_max_age_h', 'evidence_max_age_h'):
            v = policy.get(key, 6 if key.startswith('frames') else 2)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                raise ValueError(f'{key} must be a positive number')
            ages[key] = float(v)
        out = dict(enabled=enabled, **ages)
        tmp = self.run_dir/'GC.tmp'
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        tmp.replace(self.run_dir/'GC.json')
        return out

    KEEP_SUFFIX = ('-before.png', '-after.png')

    def gc(self, policy):
        if not policy or not policy.get('enabled'):
            return dict(deleted_frames=0, freed_frames_mb=0,
                        deleted_evidence=0, freed_evidence_mb=0)
        now = time.time()
        frames_age = policy.get('frames_max_age_h', 6)*3600
        evidence_age = policy.get('evidence_max_age_h', 2)*3600
        deleted_frames = freed_frames = deleted_evidence = freed_evidence = 0
        frames = self.run_dir/'frames'
        if frames.is_dir():
            for p in frames.glob('*.jpg'):
                if now-p.stat().st_mtime > frames_age:
                    freed_frames += p.stat().st_size
                    p.unlink()
                    deleted_frames += 1
        for p in self.run_dir.glob('*.png'):
            if p.name.startswith('latest-') or p.name.endswith(self.KEEP_SUFFIX):
                continue
            if now-p.stat().st_mtime > evidence_age:
                freed_evidence += p.stat().st_size
                p.unlink()
                deleted_evidence += 1
        return dict(deleted_frames=deleted_frames, freed_frames_mb=round(freed_frames/1048576, 1),
                    deleted_evidence=deleted_evidence, freed_evidence_mb=round(freed_evidence/1048576, 1))

    def data(self, tl_after=0, ev_after=0):
        state = None
        state_path = self.run_dir/'state.json'
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except json.JSONDecodeError:
                pass  # rewritten atomically; retry next poll
        recognition = {}
        names = [w.get('name') for w in (state or {}).get('config', {}).get('windows', [])
                 if w.get('name')]
        if not names:
            for path in self.run_dir.glob('recognition-*.json'):
                names.append(path.stem.removeprefix('recognition-'))
        for name in names:
            path = self.run_dir/f'recognition-{name}.json'
            if path.exists():
                try:
                    recognition[name] = json.loads(path.read_text())
                except json.JSONDecodeError:
                    pass
        timeline, tl_total = read_jsonl(self.run_dir/'timeline.jsonl', tl_after)
        events, ev_total = read_jsonl(self.run_dir/'events.jsonl', ev_after)
        return dict(state=state, recognition=recognition, timeline=timeline,
                    tl_total=tl_total, events=events, ev_total=ev_total,
                    images=self.list_images(), stopped=(self.run_dir/'STOP').exists(),
                    run_dir=str(self.run_dir))

    def list_images(self):
        names = [p.name for p in self.run_dir.glob('*.png')]
        frames = self.run_dir/'frames'
        if frames.is_dir():
            names += ['frames/'+p.name for p in frames.glob('*.jpg')]
        names = [n for n in names if self.image_path(n)]
        names.sort(key=lambda n: (self.run_dir/n).stat().st_mtime, reverse=True)
        return names[:IMAGE_CAP]

    def stop(self):
        (self.run_dir/'STOP').touch()

    def cleanup_frames(self, max_age_s=0.0):
        """Delete frames/*.jpg (the bulk storage). Evidence shots in the run
        root — state-change frames and action before/after pairs — are never
        touched. max_age_s > 0 only deletes files older than that."""
        frames = self.run_dir/'frames'
        freed, count = 0, 0
        if frames.is_dir():
            now = time.time()
            for p in sorted(frames.glob('*.jpg')):
                if max_age_s > 0 and now-p.stat().st_mtime <= max_age_s:
                    continue
                freed += p.stat().st_size
                p.unlink()
                count += 1
        return dict(deleted=count, freed_bytes=freed)

    def image_path(self, name):
        parts = name.split('/')
        if len(parts) > 2 or any(p in ('', '.', '..') or not SAFE_PART.match(p) for p in parts):
            return None
        if len(parts) == 2 and parts[0] != 'frames':
            return None
        path = self.run_dir.joinpath(*parts)
        if path.suffix.lower() not in SUFFIXES or not path.is_file():
            return None
        if not path.resolve().is_relative_to(self.run_dir.resolve()):
            return None
        return path


class Handler(BaseHTTPRequestHandler):
    viewer: Viewer = None  # bound by main()

    def log_message(self, *args):
        pass  # keep the console quiet under 1Hz polling

    def _send(self, code, body, ctype, cache='no-store'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', cache)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            page = Path(__file__).parent/'viewer.html'
            self._send(200, page.read_bytes(), 'text/html; charset=utf-8', 'no-cache')
            return
        if u.path == '/api/gc':
            self._json({'policy': self.viewer.policy()})
            return
        if u.path == '/api/data':
            q = parse_qs(u.query)
            try:
                tl = int(q.get('tl_after', ['0'])[0])
                ev = int(q.get('ev_after', ['0'])[0])
            except ValueError:
                self._json({'error': 'bad cursor'}, 400)
                return
            self._json(self.viewer.data(tl, ev))
            return
        if u.path.startswith('/img/'):
            path = self.viewer.image_path(u.path[5:])
            if path is None:
                self._json({'error': 'not found'}, 404)
                return
            self._send(200, path.read_bytes(), SUFFIXES[path.suffix.lower()])
            return
        self._json({'error': 'not found'}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/stop':
            self.viewer.stop()
            self._json({'stopped': True})
            return
        if path == '/api/cleanup':
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            try:
                opts = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._json({'error': 'bad json'}, 400)
                return
            age = opts.get('max_age_seconds', 0)
            if not isinstance(age, (int, float)) or age < 0:
                self._json({'error': 'bad max_age_seconds'}, 400)
                return
            self._json(self.viewer.cleanup_frames(age))
            return
        if path == '/api/gc':
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            try:
                opts = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._json({'error': 'bad json'}, 400)
                return
            try:
                self._json({'policy': self.viewer.set_policy(opts)})
            except ValueError as e:
                self._json({'error': str(e)}, 400)
            return
        self._json({'error': 'not found'}, 404)


def janitor(viewer: Viewer, interval_s: float = 60):
    """Daemon thread applying <run>/GC.json every minute. Without a policy
    file it never deletes anything, so old run dirs are safe by default."""
    def loop():
        while True:
            try:
                policy = viewer.policy()
                if policy and policy.get('enabled'):
                    result = viewer.gc(policy)
                    if result['deleted_frames'] or result['deleted_evidence']:
                        print(f'janitor: {result}', flush=True)
            except Exception as e:  # keep the janitor alive no matter what
                print(f'janitor error: {e}', flush=True)
            time.sleep(interval_s)
    threading.Thread(target=loop, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1', help='default loopback; 0.0.0.0 opts into LAN')
    parser.add_argument('--port', type=int, default=8788)
    args = parser.parse_args()
    Handler.viewer = Viewer(args.run_dir)
    janitor(Handler.viewer)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f'viewer: http://{args.host}:{httpd.server_address[1]}  (run dir: {args.run_dir})', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    raise SystemExit(main())
