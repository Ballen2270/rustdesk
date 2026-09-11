#!/usr/bin/env python3
"""rustdesk_mcp.py — MCP server proxying tools to the RustDesk bridge IPC.

The bridge (`src/bridge.rs`, built as `cargo build --release --bin bridge`)
keeps a headless RustDesk session alive and serves a local TCP IPC. This
process is a thin adapter: MCP tools (Claude Code / Claude Desktop speak MCP
over stdio) on one side, one-line JSON commands on the other.

If nothing is listening on the bridge port, this server LAUNCHES the bridge
itself (lazy spawn) using RUSTDESK_BRIDGE_BIN + RUSTDESK_BRIDGE_CONFIG, and
terminates it again on exit. If a bridge is already running (started manually,
or shared with the Python brain), it is reused untouched. One config file
(bridge.toml) therefore drives the whole chain, and a Claude Code session that
spawns this server owns its bridge's lifecycle.

Layout of the toolset mirrors the computer-use pattern the models already
know: screenshot -> decide -> act -> screenshot.

Env:
  RUSTDESK_BRIDGE_HOST    (default 127.0.0.1)
  RUSTDESK_BRIDGE_PORT    (default 21567)
  RUSTDESK_BRIDGE_BIN     bridge executable to lazy-spawn, e.g. target/release/bridge
                          (relative paths resolve against the repo root). Leave empty
                          to never spawn and require an already-running bridge.
  RUSTDESK_BRIDGE_CONFIG  TOML passed as `--config` when spawning (e.g. bridge.toml).

Run (after `pip install -r requirements.txt`):
  python3 mcp/rustdesk_mcp.py          # speaks MCP over stdio
"""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import anyio
from anyio import to_thread
from mcp.server.mcpserver import Image, MCPServer

from PIL import Image as PILImage

# Optional OCR backend — Apple Vision (macOS native, excellent zh-Hans on
# stylized game fonts, no model downloads). The server still runs without it;
# the ocr / tap_text tools raise a clear error telling you to install it.
try:
    import Vision  # type: ignore
    import Quartz  # type: ignore
except ImportError:  # pyobjc-framework-Vision not installed
    Vision = None  # type: ignore
    Quartz = None  # type: ignore

BRIDGE_HOST = os.environ.get("RUSTDESK_BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.environ.get("RUSTDESK_BRIDGE_PORT", "21567"))
IPC_TIMEOUT_S = 30.0
# Every screenshot is saved here as <md5-of-bytes>.<jpg|png> (content-hash
# naming: identical frames dedup to one file). Empty string disables saving.
SCREENSHOT_DIR = os.environ.get("RUSTDESK_SCREENSHOT_DIR", "~/rustdesk-screenshots")
# Sleep mode: with no bridge command for this many seconds, the bridge WE
# spawned is stopped (RustDesk session closed, remote side freed). The proxy
# stays up; the next tool call respawns the bridge and reconnects (~3-8s).
# Reused/external bridges are never put to sleep. 0 disables.
IDLE_TIMEOUT_S = float(os.environ.get("RUSTDESK_IDLE_TIMEOUT", "600"))
REPO_ROOT = Path(__file__).resolve().parent.parent


def _repo_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


BRIDGE_BIN = _repo_path(os.environ["RUSTDESK_BRIDGE_BIN"]) if os.environ.get("RUSTDESK_BRIDGE_BIN") else None
BRIDGE_CONFIG = _repo_path(os.environ["RUSTDESK_BRIDGE_CONFIG"]) if os.environ.get("RUSTDESK_BRIDGE_CONFIG") else None

server = MCPServer(name="rustdesk", version="0.1.1")


# ---- bridge process management -----------------------------------------------

_spawned: subprocess.Popen[bytes] | None = None
_spawn_lock = threading.Lock()
_last_activity = time.monotonic()


def _touch() -> None:
    """Mark 'the bridge is being used right now' (resets the idle timer)."""
    global _last_activity
    _last_activity = time.monotonic()


def _bridge_alive() -> bool:
    """Probe via _ipc_once (NOT the recovering _ipc) — _ensure_bridge itself
    calls this, so going through _ipc here would recurse into the lock."""
    try:
        _ipc_once({"cmd": "status"})
        return True
    except OSError:
        return False
    except RuntimeError:
        return True  # bridge answered (even with an error reply) — it's alive


def _ensure_bridge() -> None:
    """Reuse the bridge on the port, or lazily spawn our own."""
    global _spawned
    with _spawn_lock:
        if _bridge_alive():
            return  # already running (manual / Python brain / earlier session) — reuse
        if not (BRIDGE_BIN and BRIDGE_CONFIG):
            raise RuntimeError(
                f"nothing is listening on {BRIDGE_HOST}:{BRIDGE_PORT} and "
                "RUSTDESK_BRIDGE_BIN/RUSTDESK_BRIDGE_CONFIG are not set, so this "
                "server cannot launch the bridge. Start it manually "
                "(`./target/release/bridge --config bridge.toml`) or configure "
                "both env vars."
            )
        if not BRIDGE_BIN.exists():
            raise RuntimeError(
                f"RUSTDESK_BRIDGE_BIN does not exist: {BRIDGE_BIN}. "
                "Build it first: cargo build --release --bin bridge"
            )
        # Never let the child inherit stdio — these pipes ARE the MCP transport.
        log_path = Path(f"/tmp/rustdesk-bridge-{BRIDGE_PORT}.log")
        with open(log_path, "ab") as log:
            _spawned = subprocess.Popen(
                [str(BRIDGE_BIN), "--config", str(BRIDGE_CONFIG)],
                stdout=log,
                stderr=log,
                cwd=str(REPO_ROOT),
            )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if _bridge_alive():
                print(f"[rustdesk-mcp] spawned bridge pid={_spawned.pid} log={log_path}", file=sys.stderr)
                return
            if _spawned.poll() is not None:
                raise RuntimeError(
                    f"bridge exited with code {_spawned.returncode} during startup; see {log_path}"
                )
            time.sleep(0.2)
        raise RuntimeError(f"bridge did not become ready within 20s; see {log_path}")


@atexit.register
def _stop_spawned_bridge() -> None:
    if _spawned is not None and _spawned.poll() is None:
        _spawned.terminate()
        try:
            _spawned.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _spawned.kill()


def _bridge_pids() -> list[int]:
    """PIDs of the process LISTENING on the bridge port (our spawned one,
    a manually started one — whoever owns it)."""
    try:
        r = subprocess.run(
            ["lsof", "-ti", f"tcp:{BRIDGE_PORT}", "-sTCP:LISTEN"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return []
    return [int(x) for x in r.stdout.split()]


def _kill_bridge(timeout_s: float = 10.0) -> int:
    """Graceful stop: 'quit' over IPC (closes the session), then TERM/KILL the
    listener process(es). Returns how many were stopped."""
    global _spawned
    try:
        _ipc_once({"cmd": "quit"})
    except OSError:
        pass
    stopped = 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = _bridge_pids()
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, sig)
                stopped += 1
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and _bridge_pids():
            time.sleep(0.2)
    if _spawned is not None:
        try:
            _spawned.wait(timeout=2)
        except Exception:  # noqa: BLE001 — reaping best-effort
            pass
        _spawned = None
    return stopped


# SIGTERM does not run atexit handlers by default; route it through sys.exit so
# the spawned bridge gets cleaned up when the MCP client shuts us down.
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


def _sleep_owned_bridge(reason: str) -> None:
    """Sleep mode: stop the bridge WE spawned (never a reused/external one —
    the Python brain's bridge is not ours to kill)."""
    with _spawn_lock:
        if _spawned is None or _spawned.poll() is not None:
            return
        stopped = _kill_bridge()
        print(
            f"[rustdesk-mcp] {reason} — bridge put to sleep ({stopped} process(es)); "
            "next tool call respawns it and reconnects",
            file=sys.stderr,
        )


def _idle_watchdog() -> None:
    if IDLE_TIMEOUT_S <= 0:
        return
    interval = 30.0 if IDLE_TIMEOUT_S >= 120 else max(1.0, IDLE_TIMEOUT_S / 4)
    while True:
        time.sleep(interval)
        if _spawned is None or _spawned.poll() is not None:
            continue
        if time.monotonic() - _last_activity >= IDLE_TIMEOUT_S:
            _sleep_owned_bridge(f"idle for {IDLE_TIMEOUT_S:.0f}s")


# ---- bridge IPC -------------------------------------------------------------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("bridge closed the connection")
        buf += chunk
    return buf


def _ipc_once(request: dict[str, Any]) -> tuple[int, bytes]:
    """One command, one reply, no recovery. Returns (type, payload):
    0x01 JSON, 0x02 frame. Every bridge command resets the idle timer."""
    _touch()
    with socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), timeout=5.0) as sock:
        sock.settimeout(IPC_TIMEOUT_S)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall((json.dumps(request) + "\n").encode())
        header = _recv_exact(sock, 5)
        ty = header[0]
        (length,) = struct.unpack(">I", header[1:5])
        return ty, _recv_exact(sock, length)


def _ipc(request: dict[str, Any]) -> tuple[int, bytes]:
    """_ipc_once with auto-recovery: if the bridge is not accepting
    connections (never started, or crashed), bring one up first."""
    try:
        return _ipc_once(request)
    except OSError:
        _ensure_bridge()
        return _ipc_once(request)


def _ipc_json(request: dict[str, Any]) -> dict[str, Any]:
    ty, payload = _ipc(request)
    if ty != 0x01:
        raise RuntimeError(f"bridge replied with unexpected binary type {ty:#x}")
    reply: dict[str, Any] = json.loads(payload.decode())
    if not reply.get("ok", False):
        raise RuntimeError(str(reply.get("err", reply)))
    return reply


def _ipc_json_call(request: dict[str, Any]) -> dict[str, Any]:
    """_ipc_json as a single-arg callable, for to_thread.run_sync."""
    return _ipc_json(request)


# ---- frame helpers ----------------------------------------------------------


def _fetch_frame(wait_for_new_frame: bool, stale_timeout_s: float) -> tuple[bytes, int, int, bool, int]:
    """Fetch the latest decoded frame.

    Polls `status` until the bridge frame counter advances past the value seen
    at entry (so the capture is guaranteed post-action), then fetches the
    latest frame. If the counter never advances within the timeout the current
    frame is returned with stale=True.

    Returns (rgba_bytes, native_w, native_h, stale, seq).
    """
    before = _ipc_json({"cmd": "status"}).get("seq", 0)
    # Right after (re)connect no frame has been decoded yet (seq=0); wait for
    # the first one instead of erroring with "no frame yet".
    if before == 0:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _ipc_json({"cmd": "status"}).get("seq", 0) > 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(
                "no video frames decoded within 10s of the session being up — "
                "check the remote is sending video (bridge log: "
                f"/tmp/rustdesk-bridge-{BRIDGE_PORT}.log)"
            )
    stale = False
    if wait_for_new_frame:
        deadline = time.monotonic() + stale_timeout_s
        while time.monotonic() < deadline:
            if _ipc_json({"cmd": "status"}).get("seq", 0) > before:
                break
            time.sleep(0.03)
        else:
            stale = True
    ty, payload = _ipc({"cmd": "frame"})
    if ty != 0x02:
        raise RuntimeError(f"bridge replied with JSON where a frame was expected: {payload[:200]!r}")
    w, h = struct.unpack(">II", payload[:8])
    rgba = payload[8:]
    if len(rgba) != w * h * 4:
        raise RuntimeError(f"frame size mismatch: {len(rgba)} bytes for {w}x{h}")
    seq_after = _ipc_json({"cmd": "status"}).get("seq", 0)
    return rgba, w, h, stale, seq_after


def _encode(
    rgba: bytes,
    w: int,
    h: int,
    max_width: int,
    fmt: str,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[bytes, int, int, str]:
    """RGBA -> optional native-res crop -> downscaled JPEG (default) or PNG.

    crop is a native-coordinate rect (x, y, w, h) applied BEFORE any downscale,
    so the region of interest keeps full native detail. Returns
    (data, out_w, out_h, mime) of the cropped/scaled image."""
    img = PILImage.new("RGBA", (w, h))
    img.frombytes(rgba)
    if crop is not None:
        cx, cy, cw, ch = crop
        box = (max(0, cx), max(0, cy), min(w, cx + cw), min(h, cy + ch))
        if box[2] > box[0] and box[3] > box[1]:
            img = img.crop(box)
    if img.width > max_width:
        nh = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, nh), PILImage.LANCZOS)
    if fmt == "png":
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue(), img.width, img.height, "png"
    if img.mode == "RGBA":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue(), img.width, img.height, "jpeg"


# ---- OCR (Apple Vision) -----------------------------------------------------
# Text recognition on the decoded frame, so the model can work from text
# instead of downscaled images: cheap tokens, exact native coordinates.
# Requires: pip install pyobjc-framework-Vision pyobjc-framework-Quartz


def _ocr_available() -> bool:
    return Vision is not None


def _ocr_vision(img: PILImage.Image) -> list[dict[str, Any]]:
    """OCR a PIL image with Apple Vision. Returns text items sorted in reading
    order (top-to-bottom, left-to-right), boxes in the image's own pixel space
    (top-left origin, same convention as the mouse tools)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    data = Quartz.CFDataCreate(None, buf.getvalue(), len(buf.getvalue()))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    if src is None:
        raise RuntimeError("vision: failed to decode image for OCR")
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"vision: OCR failed: {err}")
    w, h = img.size
    items = []
    for o in req.results() or []:
        bb = o.boundingBox()
        items.append(
            {
                "text": o.text(),
                "x": int(bb.origin.x * w),
                "y": int((1 - bb.origin.y - bb.size.height) * h),
                "w": int(bb.size.width * w),
                "h": int(bb.size.height * h),
                "score": round(float(o.confidence()), 2),
            }
        )
    items.sort(key=lambda it: (round(it["y"] / 28), it["x"]))
    return items


# ---- MCP tools --------------------------------------------------------------


@server.tool()
async def status() -> str:
    """Connection status of the RustDesk bridge: connected, remote resolution,
    and the decoded-frame counter (useful for debugging a frozen stream).
    If pending_2fa is true the host is waiting for a 2FA code — ask the user
    for it and submit via submit_2fa.
    Note: after an idle-sleep wake-up the first call may briefly show
    connected=False while the session re-handshakes — re-check before
    concluding the connection is broken."""
    def _status() -> str:
        st = _ipc_json({"cmd": "status"})
        if not st.get("connected") and not st.get("pending_2fa"):
            # Just woke from sleep / restarted: the session may still be
            # handshaking — give it a moment before reporting a failure.
            time.sleep(1.5)
            st = _ipc_json({"cmd": "status"})
        out = (
            f"connected={st.get('connected')} resolution={st.get('w')}x{st.get('h')} "
            f"frame_seq={st.get('seq')}"
        )
        if st.get("pending_2fa"):
            out += " pending_2fa=True (waiting for a 2FA code from the user)"
        return out

    return await to_thread.run_sync(_status)


@server.tool()
async def submit_2fa(code: str, trust_this_device: bool = True) -> str:
    """Submit the 2FA verification code when the remote host requires 2FA
    (status shows pending_2fa=True). The 6-digit code comes from the user's
    authenticator app — ask them in conversation, they are time-sensitive.

    trust_this_device=True (default) asks the host to remember this machine,
    so future connections can skip 2FA (requires 'trusted devices' enabled on
    the host). A wrong code makes the host re-challenge — just ask again."""
    def _submit() -> str:
        _ipc_json({"cmd": "send_2fa", "code": code, "trust": trust_this_device})
        return "2FA code submitted; check status to confirm the session came up"

    return await to_thread.run_sync(_submit)


@server.tool()
async def restart_bridge() -> str:
    """Restart the bridge process so it reloads bridge.toml (connection peer /
    server / password / video quality / fps / port changes), or to recover a
    wedged session. The RustDesk session is closed gracefully ('quit'), the
    process is stopped, and a fresh one is launched with the new config."""
    def _restart() -> str:
        stopped = _kill_bridge()
        _ensure_bridge()
        return f"bridge restarted (stopped {stopped} old process(es), respawned)"

    return await to_thread.run_sync(_restart)


@server.tool()
async def stop_bridge() -> str:
    """Close the RustDesk session and stop the bridge process entirely (no
    remote control until it is started again). Use restart_bridge — or any
    other tool call, which spawns it lazily — to bring it back."""
    def _stop() -> str:
        stopped = _kill_bridge()
        return f"bridge stopped ({stopped} process(es) terminated, session closed)"

    return await to_thread.run_sync(_stop)


@server.tool(structured_output=False)
async def screenshot(
    wait_for_new_frame: bool = False,
    max_width: int = 1568,
    format: str = "jpeg",
    crop_x: int | None = None,
    crop_y: int | None = None,
    crop_w: int | None = None,
    crop_h: int | None = None,
) -> list[Any]:
    """Capture the remote screen and return it as an image.

    Coordinates for the mouse tools use the remote's NATIVE resolution (also
    reported in the text below), not the possibly-downscaled image dimensions.

    crop_x/crop_y/crop_w/crop_h: optional native-resolution rect (x, y, w, h)
    to crop to BEFORE downscaling — the region keeps full native detail while
    the image stays small (an 800x640 crop is ~1/4 of the 1920x1080 frame).
    The note reports crop_origin; to convert a pixel (px, py) of the cropped
    image back to native coordinates add the origin: (crop_origin_x + px, ...).
    Omit crop_* for a full-screen view.

    Args:
        wait_for_new_frame: Block (up to 3s) until a frame newer than the last
            one seen has been decoded — set this right after an action to see
            its effect instead of a pre-action frame.
        max_width: Downscale the image so its width stays <= this (saves tokens).
        format: "jpeg" (small, lossy) or "png" (lossless).
    """
    fmt = format if format in ("jpeg", "png") else "jpeg"
    max_width = max(256, min(int(max_width), 2560))

    def capture() -> dict[str, Any]:
        rgba, w, h, stale, seq = _fetch_frame(wait_for_new_frame, 3.0)
        crop = None
        if crop_x is not None and crop_y is not None and crop_w and crop_h:
            cx, cy = max(0, crop_x), max(0, crop_y)
            cw = max(1, min(crop_w, w - cx))
            ch = max(1, min(crop_h, h - cy))
            if cx < w and cy < h:
                crop = (cx, cy, cw, ch)
        data, ow, oh, mime = _encode(rgba, w, h, max_width, fmt, crop)
        saved = None
        if SCREENSHOT_DIR:
            d = Path(SCREENSHOT_DIR).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{hashlib.md5(data).hexdigest()}.{mime}"
            path.write_bytes(data)
            saved = str(path)
        return {"data": data, "mime": mime, "w": w, "h": h, "ow": ow, "oh": oh,
                "stale": stale, "seq": seq, "saved": saved, "crop": crop}

    r = await to_thread.run_sync(capture)
    note = (
        f"native_resolution={r['w']}x{r['h']} image={r['ow']}x{r['oh']} "
        f"frame_seq={r['seq']} stale={r['stale']}"
    )
    if r["crop"]:
        cx, cy, _, _ = r["crop"]
        note += f" crop_origin=({cx},{cy}) add_origin_to_image_coords_for_native"
    if r["saved"]:
        note += f" saved={r['saved']}"
    if r["stale"]:
        note += " (no new frame decoded within timeout — screen may not have changed yet)"
    return [Image(data=r["data"], format=r["mime"]), note]


@server.tool()
async def ocr(
    crop_x: int | None = None,
    crop_y: int | None = None,
    crop_w: int | None = None,
    crop_h: int | None = None,
) -> str:
    """OCR the remote screen and return text with native-coordinate boxes.

    Read-only text of the current frame — cheap and precise, so prefer this
    over screenshot() when you need to read text or find UI elements. Returns
    JSON: {"seq", "crop_origin", "items": [{"text","x","y","w","h","score"}, ...]}
    in native coordinates (top-left origin), sorted top-to-bottom, left-to-right.

    crop_x/crop_y/crop_w/crop_h: optional native rect to OCR only that region
    (faster, fewer false hits — e.g. crop the quiz panel area). Pair with
    tap_text() to click an item without any image processing.
    """
    def _do() -> str:
        if not _ocr_available():
            raise RuntimeError(
                "OCR unavailable — install it first: pip install "
                "pyobjc-framework-Vision pyobjc-framework-Quartz"
            )
        rgba, w, h, stale, seq = _fetch_frame(False, 3.0)
        img = PILImage.new("RGBA", (w, h))
        img.frombytes(rgba)
        ox = oy = 0
        if crop_x is not None and crop_y is not None and crop_w and crop_h:
            ox, oy = max(0, crop_x), max(0, crop_y)
            cw = max(1, min(crop_w, w - ox))
            ch = max(1, min(crop_h, h - oy))
            if ox < w and oy < h:
                img = img.crop((ox, oy, ox + cw, oy + ch))
        items = _ocr_vision(img)
        for it in items:  # boxes back to native frame coords
            it["x"] += ox
            it["y"] += oy
        return json.dumps({"seq": seq, "crop_origin": (ox, oy), "items": items}, ensure_ascii=False)

    return await to_thread.run_sync(_do)


@server.tool()
async def tap_text(text: str, index: int = 0) -> str:
    """Click the on-screen item whose OCR text contains `text` (case-insensitive).

    One round trip: OCR the current frame, pick the best-matching item, click
    its center (humanized). Use for buttons/menu entries where you know the
    label ("参加", "桂花醉", "A 新月步"). `index` picks among multiple matches
    (e.g. two "参加" buttons: 0 = first in reading order). Returns the matched
    text and the native coords clicked."""
    def _do() -> str:
        if not _ocr_available():
            raise RuntimeError(
                "OCR unavailable — install it first: pip install "
                "pyobjc-framework-Vision pyobjc-framework-Quartz"
            )
        rgba, w, h, stale, seq = _fetch_frame(False, 3.0)
        img = PILImage.new("RGBA", (w, h))
        img.frombytes(rgba)
        items = _ocr_vision(img)
        needle = text.strip().lower()
        matches = [
            it for it in items
            if needle in it["text"].lower() or it["text"].lower() in needle
        ]

        def rank(it: dict[str, Any]) -> tuple:
            t = it["text"].lower()
            return (t == needle, -abs(len(t) - len(needle)))

        matches.sort(key=rank, reverse=True)
        if not matches:
            seen = " ".join(it["text"] for it in items[:40])
            raise RuntimeError(
                f"text not found on screen: '{text}'. Visible text: {seen[:200]}"
            )
        m = matches[min(index, len(matches) - 1)]
        cx = m["x"] + m["w"] // 2
        cy = m["y"] + m["h"] // 2
        reply = _ipc_json({"cmd": "click", "x": cx, "y": cy, "button": "left", "double": False, "humanize": True})
        return (
            f"clicked '{m['text']}' at ({reply.get('x')}, {reply.get('y')}) "
            f"(box {m['x']},{m['y']} {m['w']}x{m['h']})"
        )

    return await to_thread.run_sync(_do)


@server.tool()
async def mouse_move(x: int, y: int) -> str:
    """Move the remote cursor to (x, y) in native remote coordinates.

    Humanized: the bridge travels a jittered multi-step path with randomized
    timing (never a teleport)."""
    await to_thread.run_sync(_ipc_json_call, {"cmd": "move", "x": x, "y": y})
    return f"moved to ({x}, {y})"


@server.tool()
async def click(
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
    humanize: bool = True,
) -> str:
    """Click at (x, y) in native remote coordinates.

    Args:
        button: "left", "right" or "middle".
        double: perform a double click.
        humanize: randomize the landing point (~4px) and press timing.
    """
    reply = await to_thread.run_sync(_ipc_json_call, {"cmd": "click", "x": x, "y": y, "button": button, "double": double, "humanize": humanize})
    return f"clicked {button}{' (double)' if double else ''} at ({reply.get('x')}, {reply.get('y')})"


@server.tool()
async def drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """Drag with the left button from (x1, y1) to (x2, y2), humanized
    (multi-step travel with jittered timing)."""
    await to_thread.run_sync(_ipc_json_call, {"cmd": "drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    return f"dragged ({x1}, {y1}) -> ({x2}, {y2})"


@server.tool()
async def scroll(x: int, y: int, dy: int = 0, dx: int = 0) -> str:
    """Scroll the mouse wheel at position (x, y). One unit is one wheel notch;
    dy > 0 scrolls toward the bottom of the page, dx > 0 scrolls right."""
    await to_thread.run_sync(_ipc_json_call, {"cmd": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})
    return f"scrolled ({dx}, {dy}) at ({x}, {y})"


@server.tool()
async def key(
    name: str,
    ctrl: bool = False,
    alt: bool = False,
    shift: bool = False,
    meta: bool = False,
) -> str:
    """Press a single key, optionally with modifiers (e.g. ctrl+c).

    `name` is a single character ("a", "1", "f") or a virtual-key name such as
    VK_RETURN, VK_ESCAPE, VK_TAB, VK_BACK, VK_DELETE, VK_UP, VK_DOWN, VK_LEFT,
    VK_RIGHT, VK_HOME, VK_END, VK_F1..VK_F12, VK_SPACE.
    """
    await to_thread.run_sync(_ipc_json_call, {"cmd": "key", "name": name, "alt": alt, "ctrl": ctrl, "shift": shift, "meta": meta})
    mods = "+".join(m for m, on in (("ctrl", ctrl), ("alt", alt), ("shift", shift), ("meta", meta)) if on)
    return f"pressed {mods + '+' if mods else ''}{name}"


@server.tool()
async def type_text(text: str) -> str:
    """Type a whole string on the remote (server-side injection — handles
    non-ASCII / IME text reliably, unlike per-key typing)."""
    await to_thread.run_sync(_ipc_json_call, {"cmd": "type", "text": text})
    return f"typed {len(text)} chars"


@server.tool()
async def wait(seconds: float) -> str:
    """Wait for `seconds` (e.g. for an animation or page load) before the next
    step. Use sparingly; 0.2–2.0s is usually enough."""
    seconds = max(0.0, min(float(seconds), 30.0))
    await anyio.sleep(seconds)
    return f"waited {seconds}s"


if __name__ == "__main__":
    # Warm up eagerly (spawn/reuse the bridge) so the first tool call is fast;
    # failure is non-fatal — tools will retry via _ipc and surface a real error.
    try:
        _ensure_bridge()
    except Exception as exc:  # noqa: BLE001 — best-effort warm-up
        print(f"[rustdesk-mcp] warm-up: {exc}", file=sys.stderr)
    threading.Thread(target=_idle_watchdog, name="idle-watchdog", daemon=True).start()
    server.run()  # stdio transport
