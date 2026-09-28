#!/usr/bin/env python3
"""Local web control centre prototype. It never calls RustDesk directly."""
from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from control_plane import ControlPlane


ROOT = Path(__file__).parent
PLANE = ControlPlane()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_json(self, body, status=HTTPStatus.OK):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            page = (ROOT / "web" / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif path == "/api/snapshot":
            self.send_json(PLANE.snapshot())
        elif path == "/api/health":
            self.send_json({"ok": True, "mode": "control-plane-only"})
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/runs":
                self.send_json({"run": PLANE.create_run(body).__dict__}, HTTPStatus.CREATED)
                return
            if path == "/api/skill-drafts":
                self.send_json({"draft": PLANE.create_draft(body)}, HTTPStatus.CREATED)
                return
            if path.startswith("/api/runs/") and path.endswith("/control"):
                run_id = path.split("/")[3]
                self.send_json({"run": PLANE.control(run_id, body.get("command", "")).__dict__})
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"control centre: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
