#!/bin/bash
# airplay-watch — bring the kiosk back after an AirPlay session ends.
#
# When a phone stops mirroring, uxplay leaves its fullscreen window up showing
# the last frame, so the screen looks frozen and the kiosk never reappears.
# Restarting uxplay destroys that window, which uncovers the browser again.
#
# Detection is by established TCP connections rather than by log scraping:
# uxplay buffers its stdout, so under systemd its messages can sit unflushed
# for a long time and never arrive when you need them.

set -uo pipefail

# shellcheck disable=SC1091
[ -r /etc/kiosk/kiosk.env ] && . /etc/kiosk/kiosk.env
PORT="${AIRPLAY_VIDEO_PORT:-35000}"
API_PORT="${API_PORT:-7000}"

STATE=/var/lib/kiosk/airplay_client
LOG=/var/log/kiosk.log

log() {
    printf '%s %s\n' "$(date '+%F %T')" "airplay-watch: $*" >>"$LOG" 2>/dev/null
    logger -t airplay-watch -- "$*" 2>/dev/null || true
}

mkdir -p /var/lib/kiosk
systemctl is-active --quiet airplay-video.service || exit 0

# Anything talking to the mirroring ports counts as an active session.
conns=$(ss -tn state established 2>/dev/null \
        | awk -v p=":$PORT" 'NR>1 && ($3 ~ p"$" || $3 ~ ":"(substr(p,2)+1)"$" || $3 ~ ":"(substr(p,2)+2)"$")' \
        | wc -l)

had=$(cat "$STATE" 2>/dev/null || echo 0)

if [ "$conns" -gt 0 ]; then
    if [ "$had" != 1 ]; then
        log "AirPlay session started, standing the browser down"
        # Chromium runs --kiosk fullscreen and sits on top of uxplay's window,
        # so a mirroring session is invisible until the browser is out of the
        # way. Openbox will not reorder them for us. kiosk-restore brings the
        # tabs back when the session ends.
        curl -s -m 10 -o /dev/null "http://127.0.0.1:${API_PORT}/surface/kill-all" 2>/dev/null \
            || pkill -f chromium 2>/dev/null
    fi
    echo 1 >"$STATE"
    exit 0
fi

if [ "$had" = 1 ]; then
    log "AirPlay session ended, clearing the mirror window"
    echo 0 >"$STATE"
    systemctl restart airplay-video.service
    # Give uxplay a moment to drop its window before nudging the browser back.
    sleep 3
    if [ -x /usr/local/bin/kiosk-restore.sh ]; then
        /usr/local/bin/kiosk-restore.sh >/dev/null 2>&1 &
    fi
fi
