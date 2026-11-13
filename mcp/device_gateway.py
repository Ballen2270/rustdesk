#!/usr/bin/env python3
"""device_gateway.py — ONE shared owner per device for the RustDesk MCP fleet.

Without a gateway every MCP proxy (each skill runner, each interactive
session) talks to the same bridge directly and independently. Three failures
follow from that, all seen in practice:

  * two processes interleave press/release input on one device (per-process
    locks cannot serialize across processes);
  * each proxy runs its own idle lease, so one proxy can sleep a bridge that
    another is actively using;
  * a proxy exiting kills the bridge it spawned even while other sessions
    still use it.

The gateway is the single writer for one device:

  * it OWNS the bridge process (spawn / idle-sleep / stop / recovery) — the
    gateway lease is fed by every proxy's traffic, so a quiet proxy can never
    sleep a busy device;
  * it serializes every command on the device (the bridge's global action
    lock lives here now);
  * it caches the latest decoded frame, so N workers on the same device share
    one bridge pull (and swizzle) per decoded frame;
  * it serves the bridge's own wire protocol — request: one JSON line;
    response: [type:u8][len:u32 BE][payload], 0x01 JSON / 0x02 frame — so
    clients keep the fleet's existing framing and code path.

Control commands (intercepted here, never proxied to the bridge):

  gateway_ping          -> {ok, device, pid, bridge_owned}
  gateway_status        -> non-waking snapshot (bridge_running/connected/seq)
  gateway_stop          -> stop the device's bridge (fleet stop rules)
  gateway_restart       -> restart the bridge (reloads the device TOML)
  gateway_keep_alive    -> {minutes} extend the gateway lease
  gateway_force_recover -> kill whatever listens on the bridge port
  gateway_shutdown      -> reply, stop an owned bridge, exit

The gateway exits by itself once no bridge runs and it has been quiet for
IDLE_EXIT_S; it is spawned on demand by bridge_fleet.Device (see
devices.toml `gateway_port`).

Run manually:
  python mcp/device_gateway.py --device pc1 --devices-config mcp/devices.toml
"""

from __future__ import annotations

import argparse
import atexit
import errno
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge_fleet  # noqa: E402

IDLE_EXIT_S = 300.0     # no bridge and no client traffic -> exit
ACCEPT_TIMEOUT_S = 1.0
# bridge.rs runs its io_loop once: after a disconnect the IPC keeps answering
# but the session never comes back on its own. Respawn an owned bridge only
# after it has reported no connection for this long AND clients kept talking
# to it (a quiet device is handled by the ordinary lease).
DEAD_SESSION_RECOVER_S = 60.0


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()


class Gateway:
    """One device: bridge ownership + global serialization + frame cache."""

    def __init__(self, fleet_path: Path, name: str):
        fleet = bridge_fleet.Fleet(fleet_path)
        device = fleet.get(name)
        if device.cfg.gateway_port is None:
            raise RuntimeError(
                f"device '{name}' has no gateway_port in {fleet_path} — add one "
                "before running a gateway for it"
            )
        self.name = name
        self.port = device.cfg.gateway_port
        # The gateway's own device handle speaks to the bridge DIRECTLY
        # (gateway_port cleared), otherwise it would recurse into itself.
        self.dev = bridge_fleet.Device(
            cfg=replace(device.cfg, gateway_port=None),
            bridge_bin=device.bridge_bin,
        )
        self.frame_seq: int | None = None
        self.frame_payload: bytes | None = None
        # Bridge lifetime + connection state the cached frame belongs to. seq
        # alone is not enough: it is a per-process counter that restarts at 0
        # when the bridge is respawned.
        self.frame_generation: int = -1
        self.frame_connected: bool | None = None
        self.frame_hits = 0
        self.last_activity = time.monotonic()
        self.dead_since: float | None = None
        self.dead_traffic_at = 0.0
        self.shutdown_pending = threading.Event()
        self.stopping = threading.Event()
        self.server: socket.socket | None = None

    # ---- request dispatch ----------------------------------------------

    def reply(self, request: dict[str, Any]) -> tuple[int, bytes]:
        cmd = str(request.get("cmd") or "")
        if cmd.startswith("gateway_"):
            return 0x01, self.control(cmd, request)
        self.last_activity = time.monotonic()
        try:
            # One global action lock: every proxied command (reads included)
            # is serialized device-wide, which is what makes press/release
            # pairs atomic across ALL MCP proxies sharing this device.
            with self.dev.action():
                if cmd == "frame":
                    return self.cached_frame()
                return self.dev.ipc(request)
        except (RuntimeError, OSError) as exc:
            return 0x01, _json(dict(ok=False, err=str(exc)))

    def cached_frame(self) -> tuple[int, bytes]:
        """Serve the bridge's latest frame from cache while its seq is
        unchanged; one bridge pull/swizzle per decoded frame across clients.

        The cache entry is keyed on (bridge lifetime, seq). seq is a
        per-process counter that restarts at 0 after a respawn, so the bridge
        generation must match too — otherwise a client can be served a frame
        from the previous bridge process at the same seq number. A seq that
        goes backwards or a disconnected->connected transition likewise means
        the old payload is no longer the current stream."""
        st = self.dev.ipc_json({"cmd": "status"})
        seq = int(st.get("seq") or 0)
        generation = self.dev.generation
        connected = st.get("connected")
        invalid = (
            generation != self.frame_generation
            or (self.frame_seq is not None and seq < self.frame_seq)
            or (self.frame_connected is False and connected is True)
        )
        if invalid:
            self.frame_seq = self.frame_payload = None
        self.frame_generation = generation
        self.frame_connected = connected if isinstance(connected, bool) else None
        if self.frame_payload is not None and seq == self.frame_seq:
            self.frame_hits += 1
            return 0x02, self.frame_payload
        ty, payload = self.dev.ipc({"cmd": "frame"})
        if ty == 0x02:
            self.frame_seq, self.frame_payload = seq, payload
        else:
            self.frame_seq = self.frame_payload = None
        return ty, payload

    def control(self, cmd: str, request: dict[str, Any]) -> bytes:
        self.last_activity = time.monotonic()
        try:
            if cmd == "gateway_ping":
                return _json(dict(ok=True, device=self.name, pid=os.getpid(),
                                  bridge_owned=self.dev.spawned is not None))
            if cmd == "gateway_status":
                st = self.dev.external_status()  # touch=False: never wakes
                out: dict[str, Any] = dict(
                    ok=True, device=self.name, gateway_pid=os.getpid(),
                    bridge_owned=self.dev.spawned is not None,
                    bridge_running=bool(st),
                    frame_cache_hits=self.frame_hits,
                    # Lifetime of the OWNED bridge; clients use it to tell two
                    # bridge processes with colliding seq numbers apart.
                    generation=self.dev.generation,
                )
                for key in ("connected", "pending_2fa", "w", "h", "seq", "display",
                            "target_display", "connected_for_s", "error"):
                    if key in st:
                        out[key] = st[key]
                return _json(out)
            if cmd == "gateway_stop":
                msg = self.dev.stop()
                self.frame_seq = self.frame_payload = None
                return _json(dict(ok=True, msg=msg))
            if cmd == "gateway_restart":
                msg = self.dev.restart()
                self.frame_seq = self.frame_payload = None
                return _json(dict(ok=True, msg=msg))
            if cmd == "gateway_keep_alive":
                minutes = float(request.get("minutes", 30.0))
                self.dev.keep_alive(minutes)
                return _json(dict(
                    ok=True,
                    msg=f"[{self.name}] gateway lease extended by {minutes} min",
                ))
            if cmd == "gateway_force_recover":
                self.frame_seq = self.frame_payload = None
                return _json(dict(ok=True, msg=self.dev.force_recover()))
            if cmd == "gateway_shutdown":
                # Do NOT shut down here: the reply has not been written yet,
                # and closing the listener underneath the caller turns the
                # answer into a 'bridge closed the connection' error. _conn
                # triggers the shutdown once the reply is on the wire.
                self.shutdown_pending.set()
                return _json(dict(ok=True, msg=f"[{self.name}] gateway shutting down"))
        except RuntimeError as exc:
            return _json(dict(ok=False, err=str(exc)))
        return _json(dict(ok=False, err=f"unknown gateway command: {cmd}"))

    # ---- lifecycle ------------------------------------------------------

    def shutdown(self) -> None:
        try:
            self.dev._teardown_owned()
        except Exception:  # noqa: BLE001 — shutdown best-effort
            pass
        self.stopping.set()
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass

    def _shutdown_after_reply(self) -> None:
        # Give the client a moment to read the reply before the listener and
        # the owned bridge go away (shutting down inside control() raced the
        # reply write and surfaced as ConnectionError on the client).
        time.sleep(0.1)
        self.shutdown()

    def watchdog(self) -> None:
        """Sleep an idle bridge (device-global lease), replace an owned bridge
        whose session died, and exit when nothing is left to own."""
        idle = self.dev.cfg.idle_timeout
        interval = min(30.0, max(5.0, idle/4)) if idle else 30.0
        while not self.stopping.wait(interval):
            try:
                self.dev.sleep()
            except Exception:  # noqa: BLE001 — one bad sweep must not kill the loop
                pass
            try:
                self._recover_dead_session()
            except Exception:  # noqa: BLE001 — one bad sweep must not kill the loop
                pass
            if (self.dev.spawned is None
                    and time.monotonic()-self.last_activity >= IDLE_EXIT_S):
                print(f"[gateway] {self.name}: no bridge and no traffic — exiting",
                      file=sys.stderr)
                self.shutdown()
                return

    def _recover_dead_session(self) -> None:
        """Respawn a bridge WE spawned whose RustDesk session dropped.

        bridge.rs starts its io_loop exactly once: after a disconnect the IPC
        still answers but `connected` stays false and seq freezes, so clients
        keep receiving an old frame until the idle lease expires. Only replace
        the bridge when the dead state persists while clients keep talking to
        the device; external bridges are never touched."""
        if self.dev.spawned is None or self.dev.spawned.poll() is not None:
            self.dead_since = None
            return
        try:
            st = self.dev.ipc_once({"cmd": "status"}, touch=False)
        except (OSError, RuntimeError):
            self.dead_since = None
            return
        if st.get("connected") or st.get("pending_2fa"):
            self.dead_since = None
            return
        now = time.monotonic()
        if self.dead_since is None:
            self.dead_since = now
            # Traffic that arrives after the drop is what makes a dead session
            # worth replacing; without it the ordinary lease handles the device.
            self.dead_traffic_at = self.last_activity
            return
        if now - self.dead_since < DEAD_SESSION_RECOVER_S:
            return
        if self.last_activity <= self.dead_traffic_at:
            return
        # Teardown+respawn must not race an in-flight action; if the lock is
        # held (a wedged command) retry on the next sweep.
        if not self.dev.action_lock.acquire(timeout=5.0):
            return
        try:
            self.dev.in_flight += 1
            try:
                if self.dev.spawned is None:
                    return
                st = self.dev.ipc_once({"cmd": "status"}, touch=False)
                if st.get("connected") or st.get("pending_2fa"):
                    return
                print(f"[gateway] {self.name}: session down for "
                      f">{DEAD_SESSION_RECOVER_S:.0f}s with traffic — respawning "
                      "owned bridge", file=sys.stderr)
                self.dev._teardown_owned()
                self.dev.ensure()
                self.frame_seq = self.frame_payload = None
                self.frame_generation = -1
            finally:
                self.dev.in_flight -= 1
                self.dev.touch()
                self.dev.action_lock.release()
        except (OSError, RuntimeError) as exc:
            print(f"[gateway] {self.name}: dead-session recovery failed: {exc}",
                  file=sys.stderr)
        finally:
            self.dead_since = None

    # ---- wire protocol --------------------------------------------------

    @staticmethod
    def _write(sock: socket.socket, ty: int, payload: bytes) -> None:
        sock.sendall(bytes([ty]) + struct.pack(">I", len(payload)) + payload)

    def _conn(self, conn: socket.socket) -> None:
        with conn:
            try:
                reader = conn.makefile("rb")
                for line in reader:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("request must be a JSON object")
                    except ValueError as exc:
                        self._write(conn, 0x01, _json(dict(ok=False, err=f"bad json: {exc}")))
                        continue
                    ty, payload = self.reply(request)
                    self._write(conn, ty, payload)
                    if self.shutdown_pending.is_set():
                        self.shutdown_pending.clear()
                        threading.Thread(target=self._shutdown_after_reply,
                                         daemon=True).start()
                        return
            except OSError:
                pass

    def serve(self) -> None:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.port))
        server.listen(16)
        server.settimeout(ACCEPT_TIMEOUT_S)
        self.server = server
        print(f"[gateway] {self.name} listening on 127.0.0.1:{self.port} "
              f"(bridge port {self.dev.cfg.port})", file=sys.stderr)
        while not self.stopping.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._conn, args=(conn,), daemon=True).start()
        try:
            server.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--devices-config", required=True, type=Path)
    args = parser.parse_args()
    path = (args.devices_config if args.devices_config.is_absolute()
            else REPO_ROOT/args.devices_config)
    gateway = Gateway(path.resolve(), args.device)
    atexit.register(gateway.shutdown)
    signal.signal(signal.SIGTERM, lambda *_: (gateway.shutdown(), sys.exit(0)))
    threading.Thread(target=gateway.watchdog, daemon=True,
                     name="gateway-watchdog").start()
    try:
        gateway.serve()
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            # Lost a concurrent spawn race: another proxy's gateway already
            # owns this device's port. That is success for this process — the
            # caller adopts the winner instead of failing its own proxy.
            print(f"[gateway] {args.device}: port {gateway.port} already served by "
                  "another gateway — exiting", file=sys.stderr)
            return 0
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
