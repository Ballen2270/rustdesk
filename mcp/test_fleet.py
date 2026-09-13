#!/usr/bin/env python3
"""Fleet-mode test: two dummy bridges, per-device lifecycle semantics.

Dummy peers never connect (expected) — this exercises spawn/routing, lease
logic (idle / keep_alive / in_flight / max_duration), teardown atomicity,
external-bridge reuse-only policy, strict config validation, and the
device-scoped tool registration swap. Uses ports 21601-21603 and the repo's
target/release/bridge. Exits 0 when every check passes.

Run (any python with mcp/requirements.txt installed, e.g.):
  conda run -n rustdesk-mcp python mcp/test_fleet.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "mcp"))
os.chdir(REPO)

BRIDGE_BIN = REPO / "target/release/bridge"
PORTS = (21601, 21602, 21603)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {detail}" if detail and not cond else ""))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    if not BRIDGE_BIN.exists():
        print(f"FAIL: bridge binary missing: {BRIDGE_BIN} (cargo build --release --bin bridge "
              "or download the CI artifact)")
        return 1
    busy = [p for p in PORTS if not port_free(p)]
    if busy:
        print(f"FAIL: test ports already in use: {busy} (another bridge running?)")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="rustdesk-fleet-test-"))

    # ---- test configs --------------------------------------------------------
    dev1 = write(tmp / "pc1.toml", f"""
[connection]
id = "127.0.0.1"
password = "x"
[video]
quality = "balanced"
[ipc]
port = {PORTS[0]}
""")
    dev2 = write(tmp / "pc2.toml", f"""
[connection]
id = "10.255.255.1"
password = "x"
[ipc]
port = {PORTS[1]}
""")
    dev3 = write(tmp / "pc3.toml", f"""
[connection]
id = "127.0.0.1"
password = "x"
[ipc]
port = {PORTS[2]}
""")
    fleet_toml = write(tmp / "devices.toml", f"""
bridge_bin = "target/release/bridge"

[[device]]
name = "pc1"
port = {PORTS[0]}
config = "{dev1}"
idle_timeout = 6

[[device]]
name = "pc2"
port = {PORTS[1]}
config = "{dev2}"
idle_timeout = 600
""")

    import bridge_fleet

    # ---- 1. config validation (incl. strict typing) --------------------------
    os.environ["RUSTDESK_DEVICES_CONFIG"] = str(fleet_toml)
    fleet = bridge_fleet.Fleet(fleet_toml)
    check("fleet parses 2 devices", set(fleet.devices) == {"pc1", "pc2"})

    for label, text in {
        "string autostart rejected": f'config = "{dev1}"\nautostart = "false"\n',
        "negative idle_timeout rejected": f'config = "{dev1}"\nidle_timeout = -5\n',
        "non-int port rejected": f"port = true\nconfig = \"{dev1}\"\n",
    }.items():
        p = write(tmp / "strict.toml", "[[device]]\nname = \"bad\"\n" + text)
        try:
            bridge_fleet.Fleet(p)
            check(label, False, "no error raised")
        except RuntimeError:
            check(label, True)

    for label, text in {
        "duplicate name rejected": (
            f'[[device]]\nname = "pc1"\nport = {PORTS[0]}\nconfig = "{dev1}"\n'
            f'[[device]]\nname = "pc1"\nport = {PORTS[1]}\nconfig = "{dev2}"\n'
        ),
        "child port mismatch rejected": f'[[device]]\nname = "pcX"\nport = 21699\nconfig = "{dev1}"\n',
    }.items():
        p = write(tmp / "v.toml", text)
        try:
            bridge_fleet.Fleet(p)
            check(label, False, "no error raised")
        except RuntimeError as e:
            check(label, True, str(e))

    try:
        fleet.get("nope")
        check("unknown device error lists names", False)
    except RuntimeError as e:
        check("unknown device error lists names", "pc1" in str(e) and "pc2" in str(e))

    # ---- 2. spawn both devices -----------------------------------------------
    d1, d2 = fleet.devices["pc1"], fleet.devices["pc2"]
    d1.ensure()
    d2.ensure()
    check("pc1 spawned+alive", d1.alive() and d1.spawned is not None)
    check("pc2 spawned+alive", d2.alive() and d2.spawned is not None)
    check("distinct pids", d1.spawned.pid != d2.spawned.pid)

    s1 = d1.ipc_json({"cmd": "status"})
    s2 = d2.ipc_json({"cmd": "status"})
    check("pc1 status answers", s1.get("ok") is True and "connected" in s1, json.dumps(s1)[:120])
    check("pc2 status answers", s2.get("ok") is True and "connected" in s2, json.dumps(s2)[:120])

    # ---- 3. lease logic --------------------------------------------------------
    d1.touch()
    check("fresh lease: no sleep", d1.should_sleep() is None)
    d1.last_activity -= 10
    check("idle past lease -> sleep reason", d1.should_sleep() == "idle for 6s")
    d1.keep_alive(5)
    check("keep_alive suppresses idle", d1.should_sleep() is None)
    d1.last_activity -= 10
    check("keep_alive beats idle when quiet", d1.should_sleep() is None)
    d1.extend_until = 0.0
    with d1.action():
        check("in_flight suppresses sleep", d1.should_sleep() is None)
    check("action exit renews the lease", d1.should_sleep() is None)
    d1.last_activity -= 10
    check("idle reason returns when quiet again", d1.should_sleep() == "idle for 6s")

    d1.in_flight += 1
    d1.sleep()
    check("sleep() no-op while in_flight", d1.spawned is not None and d1.alive())
    d1.in_flight -= 1
    d1.keep_alive(2)
    d1.sleep()
    check("sleep() no-op inside keep_alive", d1.spawned is not None and d1.alive())
    d1.extend_until = 0.0
    d1.last_activity -= 30
    d1.sleep()
    check("sleep() kills when truly idle", d1.spawned is None and not d1.alive())
    d1.touch()

    d1.ensure()
    check("pc1 respawns on demand", d1.alive() and d1.spawned is not None)
    d1.cfg.max_duration = 100
    d1.started_at = time.monotonic() - 200
    check("max_duration exceeded -> reason", d1.should_sleep() == "max_duration 100s reached")
    d1.keep_alive(1)
    check("keep_alive beats max_duration", d1.should_sleep() is None)
    d1.cfg.max_duration = 0
    d1.extend_until = 0.0
    d1.touch()

    # ---- 4. stop isolation (owned) --------------------------------------------
    msg = d1.stop()
    check("pc1 stop reports termination", "terminated" in msg, msg)
    time.sleep(0.3)
    check("pc1 down after stop", not d1.alive() and d1.spawned is None)
    check("pc2 still alive after pc1 stop", d2.alive() and d2.spawned is not None)

    # ---- 5. external bridge: reuse-only, stop must NOT touch it ----------------
    ext = subprocess.Popen(
        [str(BRIDGE_BIN), "--config", str(dev3)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(REPO),
    )
    time.sleep(1.0)
    ext_fleet = bridge_fleet.Fleet(write(tmp / "ext.toml", f"""
[[device]]
name = "ext"
port = {PORTS[2]}
config = "{dev3}"
"""))
    de = ext_fleet.devices["ext"]
    de.ensure()
    check("external bridge reused (not spawned)", de.spawned is None and de.alive())
    msg = de.stop()
    check("stop on external leaves it running", "untouched" in msg and ext.poll() is None, msg)
    check("external session untouched: status still answers", de.alive())

    # ---- 6. non-loopback host refuses to spawn ---------------------------------
    far = bridge_fleet.Fleet(write(tmp / "far.toml", f"""
bridge_bin = "target/release/bridge"

[[device]]
name = "far"
host = "192.168.199.199"
port = {PORTS[2]}
config = "{dev3}"
"""))
    try:
        far.devices["far"].ensure()
        check("non-loopback host refuses spawn", False, "no error raised")
    except RuntimeError as e:
        check("non-loopback host refuses spawn", "loopback" in str(e), str(e))

    # ---- 7. connect-only retry: no duplicate of delivered commands -------------
    attempts = {"n": 0}

    class RetryDev(bridge_fleet.Device):
        """First ipc_once fails at connect (never delivered) — ipc() must
        bring the bridge up and retry, succeeding on attempt 2."""

        def ensure(self):  # simulate bring-up
            return None

        def ipc_once(self, request, touch=True):  # type: ignore[override]
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise bridge_fleet._ConnectFailure("refused")
            return 0x01, b'{"ok": true}'

    r = RetryDev(cfg=d1.cfg, bridge_bin=None)
    ty, _ = r.ipc({"cmd": "status"})
    check("connect-phase failure retried and succeeded", attempts["n"] == 2 and ty == 0x01)

    class DropReplyDev(bridge_fleet.Device):
        """Command was delivered but the reply never came — retrying could
        duplicate the action, so ipc() must surface the error untouched."""

        def ensure(self):
            return None

        def ipc_once(self, request, touch=True):  # type: ignore[override]
            raise ConnectionError("bridge closed the connection")

    dr = DropReplyDev(cfg=d1.cfg, bridge_bin=None)
    try:
        dr.ipc({"cmd": "status"})
        check("post-connect failure NOT auto-retried", False, "no error raised")
    except ConnectionError:
        check("post-connect failure NOT auto-retried", True)

    # ---- 8. tool registration swap --------------------------------------------
    import rustdesk_mcp

    server = rustdesk_mcp.server
    before = set(server._tool_manager._tools)
    check("single-device mode has 14 tools", len(before) == 14, str(sorted(before)))

    fleet2 = bridge_fleet.run_fleet_server(server)
    after = set(server._tool_manager._tools)
    check("fleet registers 16 tools", len(after) == 16, str(sorted(after)))
    check("fleet adds list_devices/keep_alive", {"list_devices", "keep_alive"} <= after)
    bad = [
        n for n in ("click", "status", "screenshot", "tap_text", "type_text", "wait")
        if "device" not in inspect.signature(server._tool_manager._tools[n].fn).parameters
    ]
    check("same-name tools swapped to device-scoped versions", not bad, str(bad))

    list_fn = server._tool_manager._tools["list_devices"].fn
    ld = json.loads(asyncio.run(list_fn()))
    names = {e["device"] for e in ld}
    check("list_devices covers both", names == {"pc1", "pc2"}, str(ld)[:200])
    pc1_entry = next(e for e in ld if e["device"] == "pc1")
    pc2_entry = next(e for e in ld if e["device"] == "pc2")
    check("pc1 shows stopped in overview (was stopped in §4)", pc1_entry.get("running") is False)
    check("pc2 running in overview", pc2_entry.get("running") is True)

    # ---- 9. cleanup --------------------------------------------------------------
    fleet.stop_all_owned()
    time.sleep(0.3)
    check("all owned stopped", d1.spawned is None and d2.spawned is None)
    check("ports released", not d1.alive() and not d2.alive())
    ext.terminate()
    try:
        ext.wait(timeout=5)
    except subprocess.TimeoutExpired:
        ext.kill()
    check("external test bridge cleaned by test", ext.poll() is not None)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED:", FAILED)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
