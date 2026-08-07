#!/usr/bin/env python3
"""USB remote control listener via evdev.

Grabs the HAOBO keyboard and mouse devices exclusively so the OS
doesn't receive key presses or cursor movement.

The remote has two modes:
  - D-pad mode: OK = KEY_SELECT, Back = KEY_BACK (event7)
  - Air mouse mode: OK = BTN_LEFT, Back = BTN_RIGHT (event8)
Both are handled.

Sends button events to the web server for action execution.
Power button: hold 10 seconds for hard reboot (hardcoded safety).
Delete button: hold to record voice command, release to process.
"""

import json
import os
import signal
import subprocess
import time
import threading

import evdev
import requests
from evdev import categorize, ecodes
from selectors import DefaultSelector, EVENT_READ

DEVICE_KEYBOARD = "HAOBO Technology USB Composite Device Keyboard"
DEVICE_MOUSE = "HAOBO Technology USB Composite Device"

POWER_HOLD_SECONDS = 10
VOICE_HOLD_THRESHOLD = 0.5
SERVER_URL = f"http://127.0.0.1:{os.environ.get('REMOTE_PORT', '5000')}"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
VOICE_AUDIO_PATH = "/tmp/voice_cmd.wav"
VOICE_BUTTON = "delete"  # button used for voice recording (hold to record)

# Button map: evdev keycode -> friendly name
BUTTONS = {
    # D-pad mode
    "KEY_MUTE": "mute",
    ("KEY_MIN_INTERESTING", "KEY_MUTE"): "mute",
    "KEY_VOLUMEUP": "vol+",
    "KEY_VOLUMEDOWN": "vol-",
    "KEY_UP": "up",
    "KEY_DOWN": "down",
    "KEY_LEFT": "left",
    "KEY_RIGHT": "right",
    "KEY_SELECT": "ok",
    "KEY_BACK": "back",
    "KEY_HOMEPAGE": "home",
    "KEY_COMPOSE": "menu",
    "KEY_VOICECOMMAND": "voice",
    "KEY_PLAYPAUSE": "play",
    "KEY_PREVIOUSSONG": "prev",
    "KEY_NEXTSONG": "next",
    "KEY_PAGEUP": "ch+",
    "KEY_PAGEDOWN": "ch-",
    "KEY_BACKSPACE": "delete",
    "KEY_POWER": "power",
    "KEY_0": "0",
    "KEY_1": "1",
    "KEY_2": "2",
    "KEY_3": "3",
    "KEY_4": "4",
    "KEY_5": "5",
    "KEY_6": "6",
    "KEY_7": "7",
    "KEY_8": "8",
    "KEY_9": "9",
    # Air mouse mode (OK and Back become mouse clicks)
    ("BTN_LEFT", "BTN_MOUSE"): "ok",
    "BTN_LEFT": "ok",
    "BTN_RIGHT": "back",
}


def load_config():
    """Load config to get longpress thresholds."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def get_longpress_threshold(config, button_name):
    """Get longpress threshold for a button. Returns None if no longpress configured."""
    bc = config.get(button_name, {})
    lp = bc.get("longpress", {})
    if lp.get("actions"):
        return lp.get("seconds", 3)
    return None


def get_audio_device(config):
    """Get audio device from voice config."""
    voice_cfg = config.get("_voice", {})
    return voice_cfg.get("audio_device", "plughw:0,0")


def is_voice_enabled(config):
    """Check if voice is enabled in config."""
    voice_cfg = config.get("_voice", {})
    return voice_cfg.get("enabled", False)


def send_action(button, press_type):
    """Send button event to server in background thread."""
    def _send():
        try:
            requests.post(
                f"{SERVER_URL}/api/execute",
                json={"button": button, "type": press_type},
                timeout=5,
            )
        except Exception as e:
            print(f"  Server error: {e}")
    threading.Thread(target=_send, daemon=True).start()


def send_voice_command(audio_path):
    """Send recorded audio to server for voice processing in background thread."""
    def _send():
        try:
            resp = requests.post(
                f"{SERVER_URL}/api/voice",
                json={"audio_path": audio_path},
                timeout=30,
            )
            result = resp.json()
            print(f"  Voice result: {result}")
        except Exception as e:
            print(f"  Voice error: {e}")
    threading.Thread(target=_send, daemon=True).start()


def find_device(name):
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.name == name:
            return dev
    return None


def main():
    kbd = find_device(DEVICE_KEYBOARD)
    if not kbd:
        print(f"Keyboard device not found: {DEVICE_KEYBOARD}")
        return

    mouse = find_device(DEVICE_MOUSE)
    if mouse:
        mouse.grab()
        print(f"Grabbed mouse: {mouse.path} (cursor + clicks)")

    kbd.grab()
    print(f"Grabbed keyboard: {kbd.path}")
    print(f"\nListening... Power hold {POWER_HOLD_SECONDS}s = reboot.")
    print(f"Voice button: hold '{VOICE_BUTTON}' to record.")
    print(f"Server: {SERVER_URL}")
    print("Press Ctrl+C to stop.\n")

    selector = DefaultSelector()
    selector.register(kbd, EVENT_READ)
    if mouse:
        selector.register(mouse, EVENT_READ)

    power_down_time = None
    # Track key_down times: button_name -> monotonic time
    button_down_times = {}
    # Track buttons where longpress already fired (don't fire press on key_up)
    longpress_fired = set()
    config = load_config()
    config_mtime = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else 0

    # Voice recording state
    voice_recording = None  # arecord subprocess
    voice_start_time = None

    try:
        while True:
            for key, _ in selector.select(timeout=1):
                # Reload config if changed
                if os.path.exists(CONFIG_PATH):
                    mt = os.path.getmtime(CONFIG_PATH)
                    if mt != config_mtime:
                        config = load_config()
                        config_mtime = mt
                        print("Config reloaded.")

                for event in key.fileobj.read():
                    if event.type != ecodes.EV_KEY:
                        continue
                    ke = categorize(event)
                    name = BUTTONS.get(ke.keycode, ke.keycode)

                    # Skip non-string names (unmapped keys)
                    if not isinstance(name, str):
                        continue

                    # Voice recording on hold of VOICE_BUTTON (delete)
                    if name == VOICE_BUTTON and is_voice_enabled(config):
                        if ke.keystate == ke.key_down:
                            voice_start_time = time.monotonic()
                            # Start recording
                            audio_device = get_audio_device(config)
                            try:
                                voice_recording = subprocess.Popen(
                                    [
                                        "arecord",
                                        "-D", audio_device,
                                        "-f", "S16_LE",
                                        "-r", "16000",
                                        "-c", "1",
                                        "-t", "wav",
                                        VOICE_AUDIO_PATH,
                                    ],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                                print("Voice: recording started (hold to speak, release to stop)")
                            except Exception as e:
                                print(f"Voice: failed to start arecord: {e}")
                                voice_recording = None

                        elif ke.keystate == ke.key_up:
                            # Stop recording
                            if voice_recording is not None:
                                try:
                                    voice_recording.send_signal(signal.SIGTERM)
                                    voice_recording.wait(timeout=2)
                                except Exception:
                                    pass
                                voice_recording = None

                            held = time.monotonic() - voice_start_time if voice_start_time else 0
                            voice_start_time = None

                            if held >= VOICE_HOLD_THRESHOLD:
                                print(f"Voice: held {held:.1f}s, sending for processing")
                                send_voice_command(VOICE_AUDIO_PATH)
                            else:
                                # Short press: fire normal press action
                                print(f"Button: {name}")
                                send_action(name, "press")
                        # Ignore key_hold events (just keep recording)
                        continue

                    threshold = get_longpress_threshold(config, name)

                    if ke.keystate == ke.key_down:
                        button_down_times[name] = time.monotonic()
                        longpress_fired.discard(name)
                        if threshold is None:
                            # No longpress configured: fire immediately
                            print(f"Button: {name}")
                            send_action(name, "press")

                    elif ke.keystate == ke.key_hold:
                        # Fire longpress as soon as threshold reached
                        if threshold is not None and name not in longpress_fired:
                            down_time = button_down_times.get(name)
                            if down_time:
                                held = time.monotonic() - down_time
                                if held >= threshold:
                                    print(f"Button: {name} (longpress {held:.1f}s)")
                                    send_action(name, "longpress")
                                    longpress_fired.add(name)

                    elif ke.keystate == ke.key_up:
                        if threshold is not None:
                            # Has longpress config: fire press only if longpress didn't fire
                            if name not in longpress_fired:
                                print(f"Button: {name}")
                                send_action(name, "press")
                        button_down_times.pop(name, None)
                        longpress_fired.discard(name)

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        # Clean up voice recording if active
        if voice_recording is not None:
            try:
                voice_recording.send_signal(signal.SIGTERM)
                voice_recording.wait(timeout=2)
            except Exception:
                pass
        kbd.ungrab()
        if mouse:
            mouse.ungrab()
        selector.close()


if __name__ == "__main__":
    main()
