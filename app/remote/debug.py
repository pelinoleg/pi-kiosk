#!/usr/bin/env python3
"""Debug: show ALL events from ALL HAOBO devices."""

import evdev
from evdev import categorize, ecodes
from selectors import DefaultSelector, EVENT_READ

devices = []
for path in evdev.list_devices():
    dev = evdev.InputDevice(path)
    if "HAOBO" in dev.name:
        devices.append(dev)
        print(f"{dev.path} -- {dev.name}", flush=True)

print("\nPress OK, Back, Delete on remote...\n", flush=True)

sel = DefaultSelector()
for d in devices:
    sel.register(d, EVENT_READ)

try:
    while True:
        for key, _ in sel.select():
            dev = key.fileobj
            for ev in dev.read():
                if ev.type == ecodes.EV_KEY:
                    ke = categorize(ev)
                    state = {0: "UP", 1: "DOWN", 2: "HOLD"}.get(ke.keystate, "?")
                    print(
                        f"[{dev.path}] type=EV_KEY scancode={ke.scancode:#06x} "
                        f"keycode={ke.keycode!r} {state}",
                        flush=True,
                    )
                elif ev.type != ecodes.EV_SYN:
                    print(
                        f"[{dev.path}] type={ev.type} code={ev.code:#06x} value={ev.value}",
                        flush=True,
                    )
except KeyboardInterrupt:
    print("\nDone.", flush=True)
