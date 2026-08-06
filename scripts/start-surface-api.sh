#!/bin/bash
# Launcher for the Surface API. Installed to the project directory; the systemd
# unit sets INSTALL_DIR, and everything else is derived from it so the same file
# works for any user or path.

set -u

INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$INSTALL_DIR" || exit 1

# shellcheck disable=SC1091
[ -r /etc/kiosk/kiosk.env ] && . /etc/kiosk/kiosk.env

# shellcheck disable=SC1091
source "$INSTALL_DIR/.venv/bin/activate"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

# PulseAudio needs the session bus of the user we actually run as.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_SERVER="unix:${XDG_RUNTIME_DIR}/pulse/native"
export PULSE_COOKIE="${HOME}/.config/pulse/cookie"

# Belt and braces: the screen must never blank on a kiosk, even if .xinitrc was
# not the thing that started this X session.
xset s off 2>/dev/null
xset s noblank 2>/dev/null
xset dpms 0 0 0 2>/dev/null

exec python main.py
