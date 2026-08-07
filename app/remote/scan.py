#!/usr/bin/env python3
"""Scan all HAOBO remote buttons (evdev + hidraw)."""

import evdev
from evdev import categorize, ecodes
from selectors import DefaultSelector, EVENT_READ
import os

def main():
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if "HAOBO" in dev.name:
            devices.append(dev)
            print(f"evdev: {dev.path} -- {dev.name}", flush=True)

    hidraw_fds = {}
    for i in [1, 2, 3]:
        path = f"/dev/hidraw{i}"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            hidraw_fds[fd] = path
            print(f"hidraw: {path}", flush=True)
        except Exception as e:
            print(f"skip {path}: {e}", flush=True)

    print("\nListening... press ALL buttons. Ctrl+C to stop.\n", flush=True)

    sel = DefaultSelector()
    for d in devices:
        sel.register(d, EVENT_READ, data=("evdev", d))
    for fd, path in hidraw_fds.items():
        sel.register(fd, EVENT_READ, data=("hidraw", path))

    try:
        while True:
            for key, mask in sel.select(timeout=1):
                dtype, info = key.data
                if dtype == "evdev":
                    device = info
                    for event in device.read():
                        if event.type == ecodes.EV_KEY:
                            ke = categorize(event)
                            state = {0: "UP", 1: "DOWN", 2: "HOLD"}.get(
                                ke.keystate, "?"
                            )
                            print(
                                f"[{device.path}] code={ke.scancode:#06x} "
                                f"key={ke.keycode} {state}",
                                flush=True,
                            )
                elif dtype == "hidraw":
                    try:
                        data = os.read(key.fileobj, 64)
                        nonzero = any(b > 0 for b in data[1:])
                        if nonzero:
                            print(
                                f"[{info}] raw: {data.hex(' ')}", flush=True
                            )
                        else:
                            print(
                                f"[{info}] release: {data.hex(' ')}", flush=True
                            )
                    except BlockingIOError:
                        pass
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        sel.close()

if __name__ == "__main__":
    main()
