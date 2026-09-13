#!/usr/bin/env python3
"""bridge_fleet.py — multi-device fleet manager for rustdesk_mcp.py.

Fleet mode (RUSTDESK_DEVICES_CONFIG points at a devices.toml): ONE MCP server
controls N remote devices, each via its OWN bridge process with its own IPC
port and child TOML. The bridge binary and its IPC protocol are exactly the
single-device ones — all multiplexing happens here, in Python.

Per-device lifecycle (on-demand + renewable lease):

  * autostart=false (default): a device connects on its first tool call;
  * every tool call on a device renews its lease (idle_timeout, default 600s
    of quiet -> the bridge WE spawned is stopped, remote side freed; the next
    call respawns it, ~2-8s);
  * keep_alive(device, minutes) explicitly extends the lease past idle_timeout
    AND max_duration (for unattended remote scripts that generate no local
    tool traffic);
  * max_duration (default 0=off) is a hard cap on connection time regardless
    of activity — a safety net against "busy forever";
  * stop_bridge(device) closes the session immediately. External bridges
    (started manually, not by us) get a 'quit' but their process is never
    killed; idle-sleep likewise only ever stops bridges we spawned.

Devices are independent: an action on one never wakes, sleeps, or blocks
another; actions on the SAME device are serialized (action_lock) so
press/release sequences cannot interleave.

devices.toml (see devices.toml.example):
    bridge_bin = "target/release/bridge"   # optional; default RUSTDESK_BRIDGE_BIN

    [[device]]
    name = "pc1"                 # required, unique — the `device` tool argument
    host = "127.0.0.1"           # default loopback
    port = 21567                 # required, unique; must equal child [ipc] port
    config = "mcp/devices/pc1.toml"
    autostart = false            # warm at server startup instead of first use
    idle_timeout = 600           # lease seconds, renewed by every call; 0 = never
    max_duration = 0             # hard cap since spawn; 0 = off; keep_alive extends

The child TOMLs keep the single-device format ([connection]/[video]/[ipc]).
"""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from mcp.server.mcpserver import Image
from PIL import Image as PILImage

try:
    import Vision  # type: ignore
    import Quartz  # type: ignore
except ImportError:
    Vision = None  # type: ignore
    Quartz = None  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
IPC_TIMEOUT_S = 30.0
# Shared with rustdesk_mcp.py single-device mode (screenshots of all devices
# land in the same content-hash-named directory, prefixed by device name).
SCREENSHOT_DIR = os.environ.get("RUSTDESK_SCREENSHOT_DIR", "~/rustdesk-screenshots")


def _ocr_available() -> bool:
    return Vision is not None


def _ocr_vision(img: PILImage.Image) -> list[dict[str, Any]]:
    """OCR a PIL image with Apple Vision (same backend as rustdesk_mcp.py):
    reading-order items with boxes in the image's own pixel space."""
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


def _encode(
    rgba: bytes,
    w: int,
    h: int,
    max_width: int,
    fmt: str,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[bytes, int, int, str]:
    """RGBA -> optional native-res crop -> downscaled JPEG (default) or PNG.
    Same pipeline as rustdesk_mcp.py (duplicated on purpose: fleet mode must
    not restructure the single-device module)."""
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


class _ConnectFailure(OSError):
    """Connect-phase failure: the command was never delivered, so retrying
    (after bringing the bridge back up) cannot duplicate an action."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("bridge closed the connection")
        buf += chunk
    return buf


@dataclass
class DeviceCfg:
    name: str
    port: int
    host: str = "127.0.0.1"
    config: Path | None = None
    autostart: bool = False
    idle_timeout: float = 600.0
    max_duration: float = 0.0


@dataclass
class Device:
    """One remote peer: config + bridge process + per-device lifecycle state.

    Locks: spawn_lock guards process bring-up/teardown, action_lock serializes
    tool actions on THIS device (devices are independent of each other)."""

    cfg: DeviceCfg
    bridge_bin: Path | None
    spawned: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    spawn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    action_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    in_flight: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    extend_until: float = 0.0  # keep_alive deadline; suppresses sleep before it
    started_at: float = 0.0  # when WE spawned the bridge (max_duration anchor)

    # ---- lease ---------------------------------------------------------

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def keep_alive(self, minutes: float) -> None:
        """Extend the lease explicitly (does NOT wake a sleeping device —
        it takes effect whenever the device is next up)."""
        self.extend_until = time.monotonic() + minutes * 60.0
        self.touch()

    def _expiry_reason(self) -> str | None:
        """Pure lease check on current state (caller synchronizes). Local
        timestamps only — probing here would wake devices or reset their idle
        timers. Returns a reason string or None. Never sleeps: external
        bridges, non-running devices, devices with an operation in flight, or
        inside a keep_alive window."""
        if self.spawned is None or self.spawned.poll() is not None:
            return None
        if self.in_flight > 0:
            return None
        now = time.monotonic()
        if now < self.extend_until:
            return None
        if self.cfg.idle_timeout > 0 and now - self.last_activity >= self.cfg.idle_timeout:
            return f"idle for {self.cfg.idle_timeout:.0f}s"
        if self.cfg.max_duration > 0 and now - self.started_at >= self.cfg.max_duration:
            return f"max_duration {self.cfg.max_duration:.0f}s reached"
        return None

    def should_sleep(self) -> str | None:
        """Unsynchronized lease snapshot (tests/inspection). The watchdog uses
        sleep(), which re-checks under lock."""
        return self._expiry_reason()

    # ---- IPC -----------------------------------------------------------

    def ipc_once(self, request: dict[str, Any], touch: bool = True) -> tuple[int, bytes]:
        """One command, one reply against this device's bridge. `touch=False`
        for probes (liveness checks must not renew the lease)."""
        if touch:
            self.touch()
        try:
            sock = socket.create_connection((self.cfg.host, self.cfg.port), timeout=5.0)
        except OSError as exc:
            raise _ConnectFailure(str(exc)) from exc
        with sock:
            sock.settimeout(IPC_TIMEOUT_S)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall((json.dumps(request) + "\n").encode())
            header = _recv_exact(sock, 5)
            ty = header[0]
            (length,) = struct.unpack(">I", header[1:5])
            return ty, _recv_exact(sock, length)

    def alive(self) -> bool:
        """Probe WITHOUT touching the lease (see should_sleep)."""
        try:
            self.ipc_once({"cmd": "status"}, touch=False)
            return True
        except OSError:
            return False
        except RuntimeError:
            return True  # bridge answered (even with an error reply) — alive

    def ensure(self) -> None:
        """Reuse the bridge on our port, or lazily spawn our own."""
        with self.spawn_lock:
            if self.alive():
                return  # already running (manual / earlier session) — reuse
            if self.spawned is not None and self.spawned.poll() is None:
                # Our child is alive but not answering IPC (wedged mid-start).
                # It is ours: stop it before spawning a replacement.
                self.spawned.terminate()
                try:
                    self.spawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.spawned.kill()
                    try:
                        self.spawned.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            if not (self.bridge_bin and self.cfg.config):
                raise RuntimeError(
                    f"[{self.cfg.name}] nothing is listening on {self.cfg.host}:{self.cfg.port} "
                    f"and bridge binary/config are unavailable — check devices.toml "
                    "(bridge_bin / device.config)."
                )
            if self.cfg.host not in ("127.0.0.1", "localhost", "::1"):
                # Spawning is local; a non-loopback host must run its own bridge.
                raise RuntimeError(
                    f"[{self.cfg.name}] host {self.cfg.host} is not loopback: this fleet "
                    "spawns bridges locally, so a remote host must already be running "
                    "its bridge — start it there (or fix host) and retry"
                )
            if not self.bridge_bin.exists():
                raise RuntimeError(
                    f"[{self.cfg.name}] bridge binary does not exist: {self.bridge_bin}. "
                    "Build it first: cargo build --release --bin bridge"
                )
            if not self.cfg.config.exists():
                raise RuntimeError(f"[{self.cfg.name}] device config not found: {self.cfg.config}")
            # Never let the child inherit stdio — these pipes ARE the MCP transport.
            log_path = Path(f"/tmp/rustdesk-bridge-{self.cfg.port}.log")
            with open(log_path, "ab") as log:
                self.spawned = subprocess.Popen(
                    [str(self.bridge_bin), "--config", str(self.cfg.config)],
                    stdout=log,
                    stderr=log,
                    cwd=str(REPO_ROOT),
                )
            self.started_at = time.monotonic()
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if self.alive():
                    print(
                        f"[rustdesk-mcp] [{self.cfg.name}] spawned bridge pid={self.spawned.pid} "
                        f"port={self.cfg.port} log={log_path}",
                        file=sys.stderr,
                    )
                    return
                if self.spawned.poll() is not None:
                    raise RuntimeError(
                        f"[{self.cfg.name}] bridge exited with code {self.spawned.returncode} "
                        f"during startup; see {log_path}"
                    )
                time.sleep(0.2)
            raise RuntimeError(f"[{self.cfg.name}] bridge did not become ready within 20s; see {log_path}")

    def ipc(self, request: dict[str, Any]) -> tuple[int, bytes]:
        """ipc_once with auto-recovery — but ONLY for connect-phase failures
        (bridge down: never started, slept, crashed): the command was never
        delivered, so the retry cannot duplicate it. A failure AFTER the
        socket connected (send/recv) leaves delivery unknown — surface it and
        let the caller decide whether repeating the action is safe."""
        try:
            return self.ipc_once(request)
        except _ConnectFailure:
            self.ensure()
            return self.ipc_once(request)

    def ipc_json(self, request: dict[str, Any]) -> dict[str, Any]:
        ty, payload = self.ipc(request)
        if ty != 0x01:
            raise RuntimeError(f"[{self.cfg.name}] bridge replied with unexpected binary type {ty:#x}")
        reply: dict[str, Any] = json.loads(payload.decode())
        if not reply.get("ok", False):
            raise RuntimeError(f"[{self.cfg.name}] {reply.get('err', reply)}")
        return reply

    # ---- frame ---------------------------------------------------------

    def fetch_frame(self, wait_for_new_frame: bool, stale_timeout_s: float) -> tuple[bytes, int, int, bool, int]:
        """Latest decoded frame from THIS device (same semantics as the
        single-device _fetch_frame: poll seq, wait for first/new frame)."""
        before = self.ipc_json({"cmd": "status"}).get("seq", 0)
        if before == 0:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if self.ipc_json({"cmd": "status"}).get("seq", 0) > 0:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    f"[{self.cfg.name}] no video frames decoded within 10s of the session "
                    f"being up — check the remote is sending video (bridge log: "
                    f"/tmp/rustdesk-bridge-{self.cfg.port}.log)"
                )
        stale = False
        if wait_for_new_frame:
            deadline = time.monotonic() + stale_timeout_s
            while time.monotonic() < deadline:
                if self.ipc_json({"cmd": "status"}).get("seq", 0) > before:
                    break
                time.sleep(0.03)
            else:
                stale = True
        ty, payload = self.ipc({"cmd": "frame"})
        if ty != 0x02:
            raise RuntimeError(f"[{self.cfg.name}] bridge replied with JSON where a frame was expected")
        w, h = struct.unpack(">II", payload[:8])
        rgba = payload[8:]
        if len(rgba) != w * h * 4:
            raise RuntimeError(f"[{self.cfg.name}] frame size mismatch: {len(rgba)} bytes for {w}x{h}")
        seq_after = self.ipc_json({"cmd": "status"}).get("seq", 0)
        return rgba, w, h, stale, seq_after

    # ---- lifecycle -----------------------------------------------------

    @contextmanager
    def action(self) -> Iterator[None]:
        """Serialize a tool action on this device and keep the watchdog from
        sleeping it mid-action."""
        with self.action_lock:
            self.in_flight += 1
            try:
                yield
            finally:
                self.in_flight -= 1
                self.touch()

    def _teardown_owned(self) -> bool:
        """Graceful stop of OUR child: 'quit' (closes the remote session),
        then TERM/KILL — ALL under spawn_lock, so the process is dead and the
        port released before the device is visible as stopped (a concurrent
        ensure() must not reuse a dying process or fight over its port).
        Returns True if a live owned child was stopped."""
        with self.spawn_lock:
            proc = self.spawned
            if proc is None or proc.poll() is not None:
                self.spawned = None
                return False
            self.spawned = None
            self.extend_until = 0.0
            try:
                self.ipc_once({"cmd": "quit"}, touch=False)
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            return True

    def stop(self) -> str:
        """Explicit termination of OUR bridge: session closed ('quit') and
        the process stopped. An external bridge (one we did not spawn) is
        left completely untouched — not even 'quit', which would close a
        session other controllers may be using (reuse-only policy)."""
        with self.action_lock:
            if self.spawned is None:
                if self.alive():
                    return (
                        f"[{self.cfg.name}] external bridge is running — left untouched "
                        "(not ours to stop)"
                    )
                return f"[{self.cfg.name}] already stopped"
            self._teardown_owned()
            return f"[{self.cfg.name}] stopped (session closed, bridge process terminated)"

    def sleep(self) -> None:
        """Watchdog entry point: put OUR bridge to sleep. The expiry decision
        is re-checked under action_lock, so an action that just started or a
        keep_alive that just landed always wins the race against the kill."""
        with self.action_lock:
            reason = self._expiry_reason()
            if reason is None:
                return
            self._teardown_owned()
        print(
            f"[rustdesk-mcp] [{self.cfg.name}] {reason} — bridge put to sleep; "
            "next tool call respawns it and reconnects",
            file=sys.stderr,
        )


class Fleet:
    """Parsed + validated devices.toml; owns the watchdog and exit cleanup."""

    def __init__(self, path: Path):
        raw = tomllib.loads(path.read_text())
        bin_env = os.environ.get("RUSTDESK_BRIDGE_BIN", "")
        bin_rel = raw.get("bridge_bin", bin_env)
        bridge_bin = (Path(bin_rel) if Path(bin_rel).is_absolute() else REPO_ROOT / bin_rel) if bin_rel else None
        self.devices: dict[str, Device] = {}
        seen_ports: dict[int, str] = {}
        entries = raw.get("device", [])
        if not isinstance(entries, list) or not entries:
            raise RuntimeError(f"{path}: no [[device]] entries")
        for i, e in enumerate(entries):
            ctx = f"{path}: device[{i}]"
            raw_name = e.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise RuntimeError(f"{ctx}: 'name' must be a non-empty string")
            name = raw_name.strip()
            ctx = f"{path}: device '{name}'"
            if name in self.devices:
                raise RuntimeError(f"{path}: duplicate device name '{name}'")
            port = e.get("port")
            if isinstance(port, bool) or not isinstance(port, int) or not (0 < port < 65536):
                raise RuntimeError(f"{ctx}: 'port' must be an integer in 1..65535")
            if port in seen_ports:
                raise RuntimeError(
                    f"{ctx}: reuses port {port} already bound by '{seen_ports[port]}'"
                )
            seen_ports[port] = name
            autostart = e.get("autostart", False)
            if not isinstance(autostart, bool):
                raise RuntimeError(f"{ctx}: 'autostart' must be true or false (unquoted)")
            host = e.get("host", "127.0.0.1")
            if not isinstance(host, str) or not host.strip():
                raise RuntimeError(f"{ctx}: 'host' must be a non-empty string")

            def _num(key: str, default: float) -> float:
                v = e.get(key, default)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                    raise RuntimeError(f"{ctx}: '{key}' must be a finite number >= 0")
                return float(v)

            idle_timeout = _num("idle_timeout", 600.0)
            max_duration = _num("max_duration", 0.0)
            cfg_rel = e.get("config", "")
            config = (
                (Path(str(cfg_rel)) if Path(str(cfg_rel)).is_absolute() else REPO_ROOT / str(cfg_rel))
                if str(cfg_rel)
                else None
            )
            if config is None:
                raise RuntimeError(f"{ctx}: missing 'config'")
            if not config.exists():
                raise RuntimeError(f"{ctx}: config not found: {config}")
            child = tomllib.loads(config.read_text())
            child_port = child.get("ipc", {}).get("port", 0)
            if isinstance(child_port, bool) or not isinstance(child_port, int) or child_port != port:
                raise RuntimeError(
                    f"{ctx}: port {port} != its child toml [ipc] port {child_port} "
                    f"({config}) — the bridge would listen elsewhere than we connect"
                )
            dev = Device(
                cfg=DeviceCfg(
                    name=name,
                    port=port,
                    host=host.strip(),
                    config=config,
                    autostart=autostart,
                    idle_timeout=idle_timeout,
                    max_duration=max_duration,
                ),
                bridge_bin=bridge_bin,
            )
            self.devices[name] = dev
        self.path = path

    def get(self, name: str) -> Device:
        dev = self.devices.get(name)
        if dev is None:
            raise RuntimeError(f"unknown device '{name}'; known devices: {', '.join(self.devices)}")
        return dev

    # ---- watchdog / cleanup -------------------------------------------

    def start_watchdogs(self) -> None:
        """One watchdog thread PER DEVICE, so a wedged teardown (e.g. a bridge
        that accepts 'quit' but never replies) delays only that device's
        expiry enforcement, never the fleet's."""
        for dev in self.devices.values():
            threading.Thread(
                target=self._device_watchdog,
                args=(dev,),
                name=f"fleet-watchdog-{dev.cfg.name}",
                daemon=True,
            ).start()

    @staticmethod
    def _device_watchdog(dev: Device) -> None:
        timers = [t for t in (dev.cfg.idle_timeout, dev.cfg.max_duration) if t > 0]
        if not timers:
            return
        interval = min(30.0, max(1.0, min(timers) / 4))
        while True:
            time.sleep(interval)
            dev.sleep()

    def stop_all_owned(self) -> None:
        for dev in self.devices.values():
            if dev.spawned is not None and dev.spawned.poll() is None:
                try:
                    dev.stop()
                except Exception:  # noqa: BLE001 — shutdown best-effort
                    pass


def _register_fleet_tools(server: Any, fleet: Fleet) -> None:  # noqa: ANN401 — MCPServer from caller
    """Register device-scoped versions of the toolset. Same names as
    single-device mode (rustdesk_mcp.py removes its own registrations before
    calling this), so models keep one vocabulary; every device-facing tool
    takes `device` as its first argument."""
    from anyio import to_thread

    def dev_of(name: str) -> Device:
        return fleet.get(name)

    async def status(device: str) -> str:
        """Connection status of one device's bridge: connected, remote
        resolution, decoded-frame counter, pending_2fa flag. Wakes the device
        if it is asleep (use list_devices for a non-waking overview).
        Note: right after a wake-up the first call may briefly show
        connected=False while the session re-handshakes — re-check before
        concluding the connection is broken."""
        dev = dev_of(device)

        def _status() -> str:
            with dev.action():
                dev.ensure()
                st = dev.ipc_json({"cmd": "status"})
                if not st.get("connected") and not st.get("pending_2fa"):
                    time.sleep(1.5)
                    st = dev.ipc_json({"cmd": "status"})
            out = (
                f"device={device} connected={st.get('connected')} resolution={st.get('w')}x{st.get('h')} "
                f"frame_seq={st.get('seq')}"
            )
            if st.get("pending_2fa"):
                out += " pending_2fa=True (waiting for a 2FA code from the user)"
            return out

        return await to_thread.run_sync(_status)

    async def submit_2fa(device: str, code: str, trust_this_device: bool = True) -> str:
        """Submit the 2FA verification code for the given device when its host
        requires 2FA (its status shows pending_2fa=True). The 6-digit code
        comes from the user's authenticator app — ask them in conversation,
        codes are time-sensitive. trust_this_device=True (default) asks the
        host to remember this machine so future connections can skip 2FA."""
        dev = dev_of(device)

        def _submit() -> str:
            with dev.action():
                dev.ipc_json({"cmd": "send_2fa", "code": code, "trust": trust_this_device})
            return f"[{device}] 2FA code submitted; check status to confirm the session came up"

        return await to_thread.run_sync(_submit)

    async def restart_bridge(device: str) -> str:
        """Restart ONE device's bridge so it reloads that device's TOML
        (peer / server / password / video settings), or to recover a wedged
        session. Other devices are unaffected."""
        dev = dev_of(device)

        def _restart() -> str:
            with dev.action():
                if dev.spawned is None and dev.alive():
                    return (
                        f"[{device}] external bridge is running — not owned, reused as-is "
                        "(its TOML was NOT reloaded)"
                    )
                dev._teardown_owned()
                dev.ensure()
            return f"[{device}] bridge restarted with {dev.cfg.config}"

        return await to_thread.run_sync(_restart)

    async def stop_bridge(device: str) -> str:
        """Terminate ONE device: close its RustDesk session and stop its
        bridge (no remote control of that device until it is started again —
        any tool call on it spawns it lazily). Other devices are unaffected."""
        dev = dev_of(device)

        def _stop() -> str:
            with dev.action():
                return dev.stop()

        return await to_thread.run_sync(_stop)

    async def keep_alive(device: str, minutes: float = 30.0) -> str:
        """Extend a device's connection lease by `minutes` (default 30), beyond
        idle_timeout AND max_duration. Use when work continues WITHOUT local
        tool traffic — e.g. a long-running remote script/animation — so the
        watchdog does not sleep the device mid-work. Does not wake a sleeping
        device; it applies whenever the device is next up."""
        dev = dev_of(device)
        dev.keep_alive(minutes)
        return f"[{device}] lease extended by {minutes} min (idle + max_duration checks suspended until then)"

    async def list_devices() -> str:
        """Non-waking overview of every configured device: running / connected
        / pending_2fa, and remaining lease. Probing never spawns bridges."""
        def _probe(dev: Device) -> dict[str, Any]:
            out: dict[str, Any] = {
                "device": dev.cfg.name,
                "port": dev.cfg.port,
                "owned": dev.spawned is not None,
                "autostart": dev.cfg.autostart,
                "idle_timeout_s": dev.cfg.idle_timeout,
                "max_duration_s": dev.cfg.max_duration,
            }
            try:
                st = dev.ipc_once({"cmd": "status"}, touch=False)
                ty, payload = st
                if ty == 0x01:
                    r = json.loads(payload.decode())
                    out.update(
                        running=True,
                        connected=r.get("connected"),
                        pending_2fa=r.get("pending_2fa"),
                        resolution=f"{r.get('w')}x{r.get('h')}",
                        frame_seq=r.get("seq"),
                    )
                else:
                    out.update(running=True, connected=None)
            except (OSError, ConnectionError, RuntimeError, json.JSONDecodeError):
                out.update(running=False, connected=False)
            now = time.monotonic()
            if out.get("running") and dev.cfg.idle_timeout > 0:
                out["idle_in_s"] = round(max(0.0, dev.cfg.idle_timeout - (now - dev.last_activity)))
            if dev.extend_until > now:
                out["keep_alive_for_s"] = round(dev.extend_until - now)
            return out

        def _list() -> str:
            return json.dumps([_probe(dev) for dev in fleet.devices.values()], ensure_ascii=False)

        return await to_thread.run_sync(_list)

    async def screenshot(
        device: str,
        wait_for_new_frame: bool = False,
        max_width: int = 1568,
        format: str = "jpeg",
        crop_x: int | None = None,
        crop_y: int | None = None,
        crop_w: int | None = None,
        crop_h: int | None = None,
    ) -> list[Any]:
        """Capture the given device's remote screen and return it as an image.

        Coordinates for the mouse tools use the remote's NATIVE resolution
        (also reported in the text below), not the possibly-downscaled image
        dimensions.

        crop_x/crop_y/crop_w/crop_h: optional native-resolution rect (x, y, w,
        h) to crop to BEFORE downscaling — the region keeps full native detail
        while the image stays small. The note reports crop_origin; to convert
        a pixel (px, py) of the cropped image back to native coordinates add
        the origin: (crop_origin_x + px, ...). Omit crop_* for a full-screen
        view.

        Args:
            device: Target device name (see list_devices).
            wait_for_new_frame: Block (up to 3s) until a frame newer than the
                last one seen has been decoded — set this right after an
                action to see its effect instead of a pre-action frame.
            max_width: Downscale the image so its width stays <= this.
            format: "jpeg" (small, lossy) or "png" (lossless).
        """
        dev = dev_of(device)
        fmt = format if format in ("jpeg", "png") else "jpeg"
        max_width = max(256, min(int(max_width), 2560))

        def capture() -> dict[str, Any]:
            with dev.action():
                rgba, w, h, stale, seq = dev.fetch_frame(wait_for_new_frame, 3.0)
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
                path = d / f"{device}-{hashlib.md5(data).hexdigest()}.{mime}"
                path.write_bytes(data)
                saved = str(path)
            return {"data": data, "mime": mime, "w": w, "h": h, "ow": ow, "oh": oh,
                    "stale": stale, "seq": seq, "saved": saved, "crop": crop}

        r = await to_thread.run_sync(capture)
        note = (
            f"device={device} native_resolution={r['w']}x{r['h']} image={r['ow']}x{r['oh']} "
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

    async def ocr(
        device: str,
        crop_x: int | None = None,
        crop_y: int | None = None,
        crop_w: int | None = None,
        crop_h: int | None = None,
    ) -> str:
        """OCR the given device's screen; returns text with native-coordinate
        boxes. Read-only text of the current frame — cheap and precise, so
        prefer this over screenshot() when reading text or finding UI
        elements. Returns JSON: {"seq", "crop_origin", "items": [{"text",
        "x","y","w","h","score"}, ...]} in native coordinates, sorted
        top-to-bottom, left-to-right. crop_* limits OCR to a region (faster,
        fewer false hits). Pair with tap_text() to click an item."""
        dev = dev_of(device)

        def _do() -> str:
            if not _ocr_available():
                raise RuntimeError(
                    "OCR unavailable — install it first: pip install "
                    "pyobjc-framework-Vision pyobjc-framework-Quartz"
                )
            with dev.action():
                rgba, w, h, _, seq = dev.fetch_frame(False, 3.0)
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
            return json.dumps(
                {"device": device, "seq": seq, "crop_origin": (ox, oy), "items": items},
                ensure_ascii=False,
            )

        return await to_thread.run_sync(_do)

    async def tap_text(device: str, text: str, index: int = 0) -> str:
        """On the given device, click the on-screen item whose OCR text
        contains `text` (case-insensitive). One round trip: OCR the current
        frame, pick the best-matching item, click its center (humanized).
        Use for buttons/menu entries where you know the label. `index` picks
        among multiple matches (0 = first in reading order). Returns the
        matched text and the native coords clicked."""
        dev = dev_of(device)

        def _do() -> str:
            if not _ocr_available():
                raise RuntimeError(
                    "OCR unavailable — install it first: pip install "
                    "pyobjc-framework-Vision pyobjc-framework-Quartz"
                )
            with dev.action():
                rgba, w, h, _, seq = dev.fetch_frame(False, 3.0)
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
                        f"[{device}] text not found on screen: '{text}'. Visible text: {seen[:200]}"
                    )
                m = matches[min(index, len(matches) - 1)]
                cx = m["x"] + m["w"] // 2
                cy = m["y"] + m["h"] // 2
                reply = dev.ipc_json(
                    {"cmd": "click", "x": cx, "y": cy, "button": "left", "double": False, "humanize": True}
                )
            return (
                f"[{device}] clicked '{m['text']}' at ({reply.get('x')}, {reply.get('y')}) "
                f"(box {m['x']},{m['y']} {m['w']}x{m['h']})"
            )

        return await to_thread.run_sync(_do)

    async def mouse_move(device: str, x: int, y: int) -> str:
        """On the given device, move the remote cursor to (x, y) in native
        remote coordinates. Humanized: jittered multi-step path, randomized
        timing (never a teleport)."""
        dev = dev_of(device)

        def _do() -> None:
            with dev.action():
                dev.ipc_json({"cmd": "move", "x": x, "y": y})

        await to_thread.run_sync(_do)
        return f"[{device}] moved to ({x}, {y})"

    async def click(
        device: str,
        x: int,
        y: int,
        button: str = "left",
        double: bool = False,
        humanize: bool = True,
    ) -> str:
        """On the given device, click at (x, y) in native remote coordinates.

        Args:
            button: "left", "right" or "middle".
            double: perform a double click.
            humanize: randomize the landing point (~4px) and press timing.
        """
        dev = dev_of(device)

        def _do() -> dict[str, Any]:
            with dev.action():
                return dev.ipc_json(
                    {"cmd": "click", "x": x, "y": y, "button": button, "double": double, "humanize": humanize}
                )

        reply = await to_thread.run_sync(_do)
        return f"[{device}] clicked {button}{' (double)' if double else ''} at ({reply.get('x')}, {reply.get('y')})"

    async def drag(device: str, x1: int, y1: int, x2: int, y2: int) -> str:
        """On the given device, drag with the left button from (x1, y1) to
        (x2, y2), humanized (multi-step travel with jittered timing)."""
        dev = dev_of(device)

        def _do() -> None:
            with dev.action():
                dev.ipc_json({"cmd": "drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2})

        await to_thread.run_sync(_do)
        return f"[{device}] dragged ({x1}, {y1}) -> ({x2}, {y2})"

    async def scroll(device: str, x: int, y: int, dy: int = 0, dx: int = 0) -> str:
        """On the given device, scroll the wheel at (x, y). One unit is one
        wheel notch; dy > 0 scrolls toward the bottom, dx > 0 scrolls right."""
        dev = dev_of(device)

        def _do() -> None:
            with dev.action():
                dev.ipc_json({"cmd": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})

        await to_thread.run_sync(_do)
        return f"[{device}] scrolled ({dx}, {dy}) at ({x}, {y})"

    async def key(
        device: str,
        name: str,
        ctrl: bool = False,
        alt: bool = False,
        shift: bool = False,
        meta: bool = False,
    ) -> str:
        """On the given device, press a single key, optionally with modifiers
        (e.g. ctrl+c). `name` is a single character or a virtual-key name such
        as VK_RETURN, VK_ESCAPE, VK_TAB, VK_F1..VK_F12, VK_SPACE."""
        dev = dev_of(device)

        def _do() -> None:
            with dev.action():
                dev.ipc_json(
                    {"cmd": "key", "name": name, "alt": alt, "ctrl": ctrl, "shift": shift, "meta": meta}
                )

        await to_thread.run_sync(_do)
        mods = "+".join(m for m, on in (("ctrl", ctrl), ("alt", alt), ("shift", shift), ("meta", meta)) if on)
        return f"[{device}] pressed {mods + '+' if mods else ''}{name}"

    async def type_text(device: str, text: str) -> str:
        """On the given device, type a whole string (server-side injection —
        handles non-ASCII / IME text reliably, unlike per-key typing)."""
        dev = dev_of(device)

        def _do() -> None:
            with dev.action():
                dev.ipc_json({"cmd": "type", "text": text})

        await to_thread.run_sync(_do)
        return f"[{device}] typed {len(text)} chars"

    async def wait(seconds: float, device: str | None = None) -> str:
        """Wait `seconds` (e.g. for an animation or page load) before the next
        step. Use sparingly; 0.2–2.0s is usually enough. If `device` is given,
        the wait renews that device's lease (waiting on its screen counts as
        working on it)."""
        import anyio

        seconds = max(0.0, min(float(seconds), 30.0))
        if device is not None:
            dev_of(device).touch()
        await anyio.sleep(seconds)
        return f"waited {seconds}s"

    # Fleet tool names are identical to the single-device ones so the model's
    # vocabulary does not change — only the added `device` argument.
    for fn, structured in (
        (status, None),
        (submit_2fa, None),
        (restart_bridge, None),
        (stop_bridge, None),
        (keep_alive, None),
        (list_devices, None),
        (screenshot, False),
        (ocr, None),
        (tap_text, None),
        (mouse_move, None),
        (click, None),
        (drag, None),
        (scroll, None),
        (key, None),
        (type_text, None),
        (wait, None),
    ):
        server.add_tool(fn, structured_output=structured)


# Tool names registered by rustdesk_mcp.py at import time (decorators). Fleet
# mode replaces every one of them with a device-scoped version.
SINGLE_DEVICE_TOOLS = (
    "status", "submit_2fa", "restart_bridge", "stop_bridge",
    "screenshot", "ocr", "tap_text", "mouse_move", "click", "drag",
    "scroll", "key", "type_text", "wait",
)


def run_fleet_server(server: Any) -> Fleet:  # noqa: ANN401 — MCPServer from caller
    """Switch `server` (already carrying the single-device tools) into fleet
    mode: replace them with device-scoped versions, warm autostart devices,
    start the watchdog and exit cleanup."""
    config_env = os.environ.get("RUSTDESK_DEVICES_CONFIG", "")
    path = Path(config_env) if Path(config_env).is_absolute() else REPO_ROOT / config_env
    if not path.exists():
        raise RuntimeError(f"RUSTDESK_DEVICES_CONFIG not found: {path}")
    fleet = Fleet(path)
    for name in SINGLE_DEVICE_TOOLS:
        try:
            server.remove_tool(name)
        except Exception as exc:  # noqa: BLE001 — absent single tool is fine
            print(f"[rustdesk-mcp] note: single-device tool '{name}' not removed: {exc}", file=sys.stderr)
    _register_fleet_tools(server, fleet)
    atexit.register(fleet.stop_all_owned)
    fleet.start_watchdogs()

    def _warm(dev: Device) -> None:
        try:
            with dev.action():
                dev.ensure()
        except Exception as exc:  # noqa: BLE001 — one device failing must not block others
            print(f"[rustdesk-mcp] [{dev.cfg.name}] autostart failed: {exc}", file=sys.stderr)

    for dev in fleet.devices.values():
        if dev.cfg.autostart:
            threading.Thread(target=_warm, args=(dev,), daemon=True).start()
    return fleet


# SIGTERM -> sys.exit so atexit cleans up every owned bridge when the MCP
# client shuts us down (same pattern as rustdesk_mcp.py; harmless if the
# handler is already installed).
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
