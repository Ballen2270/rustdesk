#!/usr/bin/env python3
"""Gateway-mode test: shared per-device gateway over a fake bridge.

Exercises the properties the gateway exists for, without a remote device:
  1. two independent MCP-proxy clients (separate Device objects, separate
     action locks) get their inputs serialized by the gateway — no
     press/release interleaving across processes;
  2. the latest decoded frame is pulled from the bridge once per seq and
     shared by all clients;
  3. lifecycle control goes through the gateway: stop/restart leave an
     external (not gateway-owned) bridge untouched, keep_alive and status
     answer, and gateway_shutdown exits the process;
  4. a bridge respawned by the gateway resets seq to 0 — the frame cache must
     key on the bridge lifetime too, or clients get the old process's frame;
  5. gateway_shutdown's reply is read by the client (no shutdown-vs-reply
     race), repeated several times;
  6. concurrent gateway spawns: the losers exit/are adopted and every client
     still reaches the one gateway.

Uses ports 21622 (fake "bridge") / 21623 (gateway). Exits 0 when every check
passes.

Run:  conda run -n rustdesk-mcp python mcp/test_gateway.py
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "mcp"))

BRIDGE_PORT = 21622
GATEWAY_PORT = 21623

# Standalone fake bridge spawned by the GATEWAY (bridge_bin in devices.toml),
# so killing and respawning it exercises the production ownership path. Its
# seq/connected/marker come from a JSON state file the test rewrites.
FAKE_BRIDGE_SCRIPT = r'''#!/usr/bin/env python3
import json
import re
import socket
import struct
import sys
import threading
from pathlib import Path

STATE = Path("__STATE__")


def state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def write(conn, ty, payload):
    conn.sendall(bytes([ty]) + struct.pack(">I", len(payload)) + payload)


def handle(conn):
    with conn:
        try:
            for line in conn.makefile("rb"):
                req = json.loads(line)
                cmd = req.get("cmd")
                st = state()
                if cmd == "status":
                    write(conn, 0x01, json.dumps(dict(
                        ok=True,
                        connected=bool(st.get("connected", True)),
                        w=8, h=8, seq=int(st.get("seq", 0)),
                        pending_2fa=False,
                        display=int(st.get("display", 0)),
                        target_display=int(st.get("target_display", 0)),
                        connected_for_s=st.get("connected_for_s"),
                        error=st.get("error"),
                    )).encode())
                elif cmd == "frame":
                    marker = int(st.get("marker", 0)) & 0xFF
                    write(conn, 0x02, struct.pack(">II", 8, 8)
                          + bytes([marker]) + b"\0" * (8 * 8 * 4 - 1))
                elif cmd == "quit":
                    write(conn, 0x01, b'{"ok": true}')
                    return
                else:
                    write(conn, 0x01, b'{"ok": true}')
        except (OSError, ValueError):
            pass


def main():
    cfg = Path(sys.argv[sys.argv.index("--config") + 1]).read_text()
    port = int(re.search(r"port\s*=\s*(\d+)", cfg).group(1))
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(16)
    while True:
        conn, _ = sock.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
'''


def wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {detail}" if detail and not cond else ""))


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


class FakeBridge(threading.Thread):
    """Bridge wire protocol over a fake peer: status/frame/click/quit."""

    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port = port
        self.events: list[str] = []      # enter/exit around each input command
        self.frame_fetches = 0
        self.clicks = 0
        self.seq = 1
        self.quit = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.sock.settimeout(0.5)

    @staticmethod
    def _write(conn: socket.socket, ty: int, payload: bytes) -> None:
        conn.sendall(bytes([ty]) + struct.pack(">I", len(payload)) + payload)

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                for line in conn.makefile("rb"):
                    req = json.loads(line)
                    cmd = req.get("cmd")
                    if cmd == "status":
                        self._write(conn, 0x01, json.dumps(
                            dict(ok=True, connected=True, w=8, h=8, seq=self.seq,
                                 pending_2fa=False)).encode())
                    elif cmd == "frame":
                        self.frame_fetches += 1
                        self._write(conn, 0x02, struct.pack(">II", 8, 8) + b"\0" * (8 * 8 * 4))
                    elif cmd in ("click", "move", "key"):
                        self.events.append("enter")
                        time.sleep(0.12)  # a slow input: overlap would interleave
                        self.events.append("exit")
                        if cmd == "click":
                            self.clicks += 1
                        self._write(conn, 0x01, json.dumps(
                            dict(ok=True, x=req.get("x", 0), y=req.get("y", 0))).encode())
                    elif cmd == "quit":
                        self.quit.set()
                        self._write(conn, 0x01, b'{"ok": true}')
                        return
                    else:
                        self._write(conn, 0x01, b'{"ok": true}')
            except (OSError, ValueError):
                pass

    def run(self) -> None:
        while not self.quit.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def close(self) -> None:
        self.quit.set()
        try:
            self.sock.close()
        except OSError:
            pass


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def main() -> int:
    busy = [p for p in (BRIDGE_PORT, GATEWAY_PORT) if not port_free(p)]
    if busy:
        print(f"FAIL: test ports already in use: {busy}")
        return 1
    os.environ.pop("RUSTDESK_BRIDGE_BIN", None)

    tmp = Path(tempfile.mkdtemp(prefix="rustdesk-gateway-test-"))
    child = write(tmp / "fake.toml", f"""
[connection]
id = "127.0.0.1"
password = "x"
[ipc]
port = {BRIDGE_PORT}
""")
    fleet_toml = write(tmp / "devices.toml", f"""
[[device]]
name = "fake"
port = {BRIDGE_PORT}
gateway_port = {GATEWAY_PORT}
config = "{child}"
idle_timeout = 600
""")
    direct_toml = write(tmp / "direct.toml", f"""
[[device]]
name = "direct"
port = {BRIDGE_PORT}
config = "{child}"
""")

    import bridge_fleet

    fake = FakeBridge(BRIDGE_PORT)
    fake.start()
    check("fake bridge listening", not port_free(BRIDGE_PORT))

    try:
        # ---- 1. gateway spawn + transparent proxying --------------------------
        fleet = bridge_fleet.Fleet(fleet_toml)
        dev = fleet.devices["fake"]
        check("device runs in gateway mode", dev.use_gateway)
        check("direct config stays gateway-free",
              not bridge_fleet.Fleet(direct_toml).devices["direct"].use_gateway)

        dev.ensure()
        check("gateway spawned", dev.gateway_spawned is not None
              and dev.gateway_spawned.poll() is None and dev._gw_alive())
        st = dev.ipc_json({"cmd": "status"})
        check("status proxied through gateway", st.get("connected") is True, str(st))

        # ---- 2. cross-client input serialization -----------------------------
        client_a = bridge_fleet.Fleet(fleet_toml).devices["fake"]
        client_b = bridge_fleet.Fleet(fleet_toml).devices["fake"]
        errors: list[BaseException] = []

        def click_n(client: bridge_fleet.Device, n: int) -> None:
            try:
                for _ in range(n):
                    client.ipc_json({"cmd": "click", "x": 1, "y": 2})
            except BaseException as exc:  # noqa: BLE001 — surface in main thread
                errors.append(exc)

        threads = [threading.Thread(target=click_n, args=(client_a, 3)),
                   threading.Thread(target=click_n, args=(client_b, 3))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        check("no client errors", not errors, str(errors))
        check("all clicks delivered", fake.clicks == 6, str(fake.clicks))
        # Serialized means enter/exit pairs never interleave. Without the
        # gateway the two clients' locks are independent -> e/e/x/x.
        check("inputs serialized across clients",
              fake.events == ["enter", "exit"] * 6, str(fake.events[:12]))

        # ---- 3. shared frame cache -------------------------------------------
        fake.frame_fetches = 0
        ty1, payload1 = dev.ipc({"cmd": "frame"})
        ty2, payload2 = dev.ipc({"cmd": "frame"})
        check("frame replies are binary RGBA", ty1 == 0x02 and ty2 == 0x02 and payload1 == payload2)
        check("one bridge pull per decoded frame", fake.frame_fetches == 1,
              str(fake.frame_fetches))
        fake.seq += 1
        dev.ipc({"cmd": "frame"})
        check("new seq refetches", fake.frame_fetches == 2, str(fake.frame_fetches))

        # ---- 4. lifecycle control --------------------------------------------
        status = dev.gateway_status()
        check("gateway_status reports bridge", status.get("bridge_running") is True
              and status.get("connected") is True, str(status))
        keep = dev._gw_call({"cmd": "gateway_keep_alive", "minutes": 5})
        check("keep_alive routed to gateway", keep.get("ok") is True)
        msg = dev.stop()
        check("external connected bridge left untouched", "untouched" in msg, msg)
        check("fake bridge survives stop()", fake.sock.fileno() != -1 and not fake.quit.is_set())
        msg = dev.restart()
        check("restart reuses connected external bridge", "reused as-is" in msg, msg)

        # ---- 5. shutdown ------------------------------------------------------
        dev._gw_call({"cmd": "gateway_shutdown"})
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and dev.gateway_spawned.poll() is None:
            time.sleep(0.1)
        check("gateway exits on shutdown", dev.gateway_spawned.poll() is not None)
        check("gateway port released", port_free(GATEWAY_PORT))
        check("external bridge still running after gateway exit", not fake.quit.is_set())

        # ---- 6. respawned bridge: cache keyed on (generation, seq) -----------
        # The gateway now owns the bridge in this phase (production path), so
        # killing it forces a respawn whose in-process seq restarts at 0.
        fake.close()
        check("external fake bridge released its port",
              wait_until(lambda: port_free(BRIDGE_PORT), 5.0))
        state_path = tmp / "bridge-state.json"
        marker_script = write(
            tmp / "fake_bridge.py",
            FAKE_BRIDGE_SCRIPT.replace("__STATE__", str(state_path)),
        )
        os.chmod(marker_script, 0o755)

        def write_state(seq: int, marker: int, connected: bool = True) -> None:
            state_path.write_text(json.dumps(dict(
                seq=seq, marker=marker, connected=connected,
                display=0, target_display=0, connected_for_s=30.0)))

        spawn_toml = write(tmp / "spawn_devices.toml", f"""
bridge_bin = "{marker_script}"

[[device]]
name = "respawn"
port = {BRIDGE_PORT}
gateway_port = {GATEWAY_PORT}
config = "{child}"
idle_timeout = 600
""")
        rdev = bridge_fleet.Fleet(spawn_toml).devices["respawn"]
        write_state(7, 17)
        st = rdev.ipc_json({"cmd": "status"})
        check("gateway spawned bridge #1", st.get("seq") == 7, str(st))
        ty, payload = rdev.ipc({"cmd": "frame"})
        check("bridge #1 frame cached", ty == 0x02 and payload[8] == 17,
              f"type={ty:#x} marker={payload[8:9]!r}")
        for pid in bridge_fleet._listener_pids(BRIDGE_PORT):
            os.kill(pid, signal.SIGKILL)
        check("bridge #1 killed",
              wait_until(lambda: not bridge_fleet._listener_pids(BRIDGE_PORT), 5.0))
        # Bridge #2 replays seq=7 (its counter restarted) with new pixels.
        write_state(7, 34)
        ty, payload = rdev.ipc({"cmd": "frame"})
        check("respawned bridge serves its own frame, not the cached one",
              ty == 0x02 and payload[8] == 34,
              f"type={ty:#x} marker={payload[8:9]!r} (17 = stale cache hit)")
        check("gateway_status reports the bridge generation",
              isinstance(rdev.gateway_status().get("generation"), int),
              str(rdev.gateway_status()))

        # ---- 7. gateway_shutdown replies reach the client (repeated) ---------
        shutdown_replies = 0
        shutdown_released = True
        for _ in range(5):
            client = bridge_fleet.Fleet(spawn_toml).devices["respawn"]
            client.ensure()
            try:
                reply = client._gw_call({"cmd": "gateway_shutdown"}, timeout=10.0)
                shutdown_replies += 1 if reply.get("ok") else 0
            except (OSError, RuntimeError):
                pass
            shutdown_released &= wait_until(lambda: port_free(GATEWAY_PORT), 10.0)
        check("gateway_shutdown reply delivered every time", shutdown_replies == 5,
              f"{shutdown_replies}/5")
        check("gateway_shutdown releases the port every time", shutdown_released)

        # ---- 8. concurrent spawns: losers adopt the winner -------------------
        clients = [bridge_fleet.Fleet(spawn_toml).devices["respawn"] for _ in range(3)]
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(clients))

        def boot(client: bridge_fleet.Device) -> None:
            try:
                barrier.wait(timeout=10)
                client.ensure()
            except BaseException as exc:  # noqa: BLE001 — surface in main thread
                errors.append(exc)

        threads = [threading.Thread(target=boot, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)
        check("concurrent spawn: no client crashed", not errors, str(errors))
        listeners = bridge_fleet._listener_pids(GATEWAY_PORT)
        check("concurrent spawn: exactly one gateway listens", len(listeners) == 1,
              str(listeners))
        check("concurrent spawn: every client reaches the gateway",
              all(c._gw_alive() for c in clients))
        try:
            clients[0]._gw_call({"cmd": "gateway_shutdown"}, timeout=10.0)
        except (OSError, RuntimeError):
            pass
        check("concurrent spawn: cleanup released the port",
              wait_until(lambda: port_free(GATEWAY_PORT), 10.0))
    finally:
        fake.close()
        for port in (BRIDGE_PORT, GATEWAY_PORT):
            for pid in bridge_fleet._listener_pids(port):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED:", FAILED)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
