#!/usr/bin/env python3
"""Diagnose macOS stuck-left-click (Magic Mouse after wake).

Read-only snapshot of the system's mouse-button state; no events are posted
unless --fix is given. Run with a python that has pyobjc (the rustdesk-mcp
conda env does):

  /opt/homebrew/Caskroom/miniconda/base/envs/rustdesk-mcp/bin/python tools/mouse_diag.py            # snapshot
  .../python tools/mouse_diag.py --watch     # click LEFT x3 + RIGHT x1 during the 8s window
  .../python tools/mouse_diag.py --fix       # post a synthetic LeftMouseUp (harmless if already up)

Interpretation:
  snapshot shows left_down=True while you are not touching the button
    -> system stuck in left-down/drag state; --fix should recover instantly.
  --watch: left_down/up arrive=YES but clicks still do nothing
    -> events reach the system, interpretation is stuck (same stuck-drag).
  --watch: left events arrive=no while right events arrive=YES
    -> left events die at the HID/Bluetooth layer; toggle the Magic Mouse off/on.
"""
import sys
import time

import Quartz

SOURCES = {
    "combined": Quartz.kCGEventSourceStateCombinedSessionState,
    "hid": Quartz.kCGEventSourceStateHIDSystemState,
    "private": Quartz.kCGEventSourceStatePrivate,
}
BUTTONS = {"left": Quartz.kCGMouseButtonLeft, "right": Quartz.kCGMouseButtonRight}


def snapshot() -> None:
    for src_name, src in SOURCES.items():
        marks = "  ".join(
            f"{name}_down={'DOWN' if Quartz.CGEventSourceButtonState(src, btn) else 'up   '}"
            for name, btn in BUTTONS.items()
        )
        print(f"  {src_name:9s}: {marks}")


def seconds_since(src: int, event_type: int) -> float:
    return Quartz.CGEventSourceSecondsSinceLastEventType(src, event_type)


def watch() -> None:
    src = Quartz.kCGEventSourceStateHIDSystemState
    types = {
        "left_down": Quartz.kCGEventLeftMouseDown,
        "left_up": Quartz.kCGEventLeftMouseUp,
        "left_dragged": Quartz.kCGEventLeftMouseDragged,
        "right_down": Quartz.kCGEventRightMouseDown,
        "right_up": Quartz.kCGEventRightMouseUp,
    }
    base = {name: seconds_since(src, et) for name, et in types.items()}
    print("NOW: click LEFT 3 times, then RIGHT once (8s window)...")
    time.sleep(8)
    print(f"  {'event':13s}{'age@start':>11s}{'age@now':>10s}  arrived?")
    for name, et in types.items():
        age0, age1 = base[name], seconds_since(src, et)
        print(f"  {name:13s}{age0:11.2f}{age1:10.2f}  {'YES' if age1 < age0 else 'no'}")


def fix() -> None:
    pos = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    ev = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, pos, Quartz.kCGMouseButtonLeft
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    print("posted synthetic LeftMouseUp; snapshot now:")
    snapshot()


if __name__ == "__main__":
    print("System button-state snapshot:")
    snapshot()
    if "--watch" in sys.argv:
        watch()
        print("snapshot after clicks:")
        snapshot()
    if "--fix" in sys.argv:
        fix()
