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
        if urlparse(self.path).path == '/api/stop':
            self.viewer.stop()
            self._json({'stopped': True})
            return
        self._json({'error': 'not found'}, 404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1', help='default loopback; 0.0.0.0 opts into LAN')
    parser.add_argument('--port', type=int, default=8788)
    args = parser.parse_args()
    Handler.viewer = Viewer(args.run_dir)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f'viewer: http://{args.host}:{httpd.server_address[1]}  (run dir: {args.run_dir})', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    raise SystemExit(main())
