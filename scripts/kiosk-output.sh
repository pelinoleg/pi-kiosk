#!/bin/bash
# kiosk-output — use the big screen when it is plugged in, the small one when
# it is not, and never light both at once.
#
# A Pi with a DSI panel attached keeps that panel permanently connected, so
# plugging in HDMI otherwise gives you two lit screens showing the same thing.
# This picks one: HDMI if a cable is present, the DSI panel otherwise, and
# powers the loser down - including its backlight, which is the point on a DSI
# panel that would otherwise sit there glowing.
#
# Runs from .xinitrc at session start and from kiosk-output.timer thereafter,
# so hotplugging a monitor is picked up without touching anything.

set -uo pipefail

# shellcheck disable=SC1091
[ -r /etc/kiosk/kiosk.env ] && . /etc/kiosk/kiosk.env

export DISPLAY="${DISPLAY:-:0}"
[ -n "${XAUTHORITY:-}" ] || export XAUTHORITY="$HOME/.Xauthority"

STATE=/var/lib/kiosk/active_output
LOG=/var/log/kiosk.log

log() {
    printf '%s %s\n' "$(date '+%F %T')" "kiosk-output: $*" >>"$LOG" 2>/dev/null
    logger -t kiosk-output -- "$*" 2>/dev/null || true
}

# The panel was switched off on purpose - by hand over the API or by the
# night schedule. Re-enabling a mode-less output is exactly this script's job,
# so without this check it silently lit the screen back up on the next tick.
[ -e /var/lib/kiosk/display_off ] && exit 0

query=$(xrandr --query 2>/dev/null) || exit 0
[ -n "$query" ] || exit 0

connected=$(printf '%s\n' "$query" | awk '/ connected/{print $1}')
[ -n "$connected" ] || exit 0

# Prefer HDMI when a cable is actually in it; fall back to whatever is left.
want=""
for o in $connected; do
    case "$o" in HDMI*) want="$o"; break ;; esac
done
[ -n "$want" ] || want=$(printf '%s\n' "$connected" | head -1)

previous=$(cat "$STATE" 2>/dev/null || echo "")

# Bring up the chosen output, and only then drop the others: turning everything
# off first would briefly leave X with no CRTC at all, which some drivers
# resent.
if ! printf '%s\n' "$query" | grep -q "^$want connected.*[0-9]\+x[0-9]\++[0-9]\++[0-9]\+"; then
    xrandr --output "$want" --auto --primary 2>/dev/null
fi

for o in $connected; do
    [ "$o" = "$want" ] && continue
    if printf '%s\n' "$query" | grep -q "^$o connected.*[0-9]\+x[0-9]\++[0-9]\++[0-9]\+"; then
        xrandr --output "$o" --off 2>/dev/null
        log "disabled $o"
    fi
done

# On a DSI panel xrandr normally takes the backlight down with it, but not every
# driver does; make sure the small screen is actually dark when HDMI won.
case "$want" in
    HDMI*)
        for bl in /sys/class/backlight/*/; do
            [ -w "${bl}bl_power" ] && echo 4 >"${bl}bl_power" 2>/dev/null
        done
        ;;
    *)
        for bl in /sys/class/backlight/*/; do
            [ -w "${bl}bl_power" ] && echo 0 >"${bl}bl_power" 2>/dev/null
        done
        ;;
esac

if [ "$want" != "$previous" ]; then
    echo "$want" >"$STATE"
    log "active output is now $want"
    # main.py resolves the output once at import, so it has to be told.
    systemctl restart surface-api.service 2>/dev/null \
        || sudo -n systemctl restart surface-api.service 2>/dev/null || true
fi
