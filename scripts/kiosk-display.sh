#!/bin/bash
# kiosk-display on|off — switch the panel, for the night schedule or by hand.
#
# Prefers the control API so the kiosk's own view of the display stays correct,
# and falls back to driving xrandr directly if the API is not answering. On a
# DSI panel `xrandr --off` also powers the backlight down (bl_power goes to 4),
# so this genuinely darkens the screen rather than painting it black - but the
# backlight is nudged explicitly too, for panels whose driver does not follow.

set -uo pipefail

# shellcheck disable=SC1091
[ -r /etc/kiosk/kiosk.env ] && . /etc/kiosk/kiosk.env
API_PORT="${API_PORT:-7000}"
LOG=/var/log/kiosk.log

action="${1:-}"
case "$action" in
    on|off) ;;
    *) echo "usage: $0 on|off" >&2; exit 2 ;;
esac

log() {
    printf '%s %s\n' "$(date '+%F %T')" "kiosk-display: $*" >>"$LOG" 2>/dev/null
    logger -t kiosk-display -- "$*" 2>/dev/null || true
}

# Mark the OFF as intentional before touching anything, so kiosk-output does
# not relight the panel on its next tick. The API does the same when called
# directly; doing it here too covers the xrandr fallback path. The ON case
# clears the flag only after something actually turned the panel on - clearing
# it up front would strip the quiet-mode mark while quiet hours still hold.
if [ "$action" = off ]; then
    date +%s >/var/lib/kiosk/display_off 2>/dev/null || true
fi

code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' \
       "http://127.0.0.1:${API_PORT}/surface/display-${action}" 2>/dev/null)

# 409 is not "API down", it is quiet hours saying no. Falling back to raw
# xrandr here would punch straight through the quiet mode.
if [ "$code" = "409" ]; then
    log "display $action refused: quiet hours"
    exit 0
fi

if [ "$code" != "200" ]; then
    log "API returned ${code:-000}, falling back to xrandr"
    export DISPLAY="${DISPLAY:-:0}"
    user=$(stat -c %U /var/lib/kiosk 2>/dev/null)
    [ -n "${XAUTHORITY:-}" ] || export XAUTHORITY="$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)/.Xauthority"
    out=$(xrandr --query 2>/dev/null | awk '/ connected/{print $1; exit}')
    if [ -n "$out" ]; then
        if [ "$action" = off ]; then
            xrandr --output "$out" --off 2>/dev/null
        else
            xrandr --output "$out" --auto 2>/dev/null
        fi
    fi
fi

# Belt and braces on the backlight itself. The kiosk user is normally in the
# `video` group, which owns these files, so no privilege escalation is needed.
for bl in /sys/class/backlight/*/; do
    [ -w "${bl}bl_power" ] || continue
    if [ "$action" = off ]; then
        echo 4 >"${bl}bl_power" 2>/dev/null
    else
        echo 0 >"${bl}bl_power" 2>/dev/null
    fi
done

if [ "$action" = on ]; then
    rm -f /var/lib/kiosk/display_off 2>/dev/null || true
fi

log "display $action (API HTTP ${code:-000})"
