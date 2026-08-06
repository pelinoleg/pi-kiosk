#!/bin/bash
# kiosk-restore — after boot, put the screen back on whatever it was showing.
#
# COMBO_URL is the single source of truth for "what belongs on screen". It is
# expected to redirect to a call against this box's own API, so we follow
# redirects and then replay the target over loopback — that way the restore
# still works if our LAN address changed. The resolved target is cached, so a
# reboot survives the case where the box that hosts COMBO_URL comes up later
# than we do.
#
# Tunables live in /etc/kiosk/kiosk.env.

set -uo pipefail

# shellcheck disable=SC1091
[ -r /etc/kiosk/kiosk.env ] && . /etc/kiosk/kiosk.env

COMBO_URL="${COMBO_URL-}"
API_PORT="${API_PORT:-7000}"
WAIT_NETWORK="${RESTORE_WAIT_NETWORK:-180}"
WAIT_API="${RESTORE_WAIT_API:-90}"
PROBE_HOSTS="${PROBE_HOSTS:-192.168.1.1}"

STATE_DIR=/var/lib/kiosk
CACHE="$STATE_DIR/last-surface.url"
LOG=/var/log/kiosk.log

log() {
    printf '%s %s\n' "$(date '+%F %T')" "kiosk-restore: $*" >>"$LOG"
    logger -t kiosk-restore -- "$*" 2>/dev/null || true
}

lan_up() {
    local h
    for h in $PROBE_HOSTS; do
        ping -c 1 -W 2 "$h" >/dev/null 2>&1 && return 0
    done
    return 1
}

mkdir -p "$STATE_DIR"

if [ -z "$COMBO_URL" ]; then
    log "COMBO_URL is empty, display restore disabled"
    exit 0
fi

# 1. Wait for the LAN.
deadline=$(( $(date +%s) + WAIT_NETWORK ))
until lan_up; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "no LAN after ${WAIT_NETWORK}s, continuing anyway"
        break
    fi
    sleep 5
done

# 2. Wait for our own API to start answering.
deadline=$(( $(date +%s) + WAIT_API ))
until curl -s -m 5 -o /dev/null "http://127.0.0.1:${API_PORT}/" 2>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "local API silent after ${WAIT_API}s, trying anyway"
        break
    fi
    sleep 3
done

# 3. Resolve the redirect, remembering it for next time.
target=$(curl -s -m 15 -o /dev/null -w '%{redirect_url}' "$COMBO_URL" 2>/dev/null)
if [ -n "$target" ]; then
    printf '%s\n' "$target" >"$CACHE"
    log "resolved to $target"
else
    target=$(cat "$CACHE" 2>/dev/null)
    if [ -n "$target" ]; then
        log "COMBO_URL unreachable, using cached target"
    else
        # No redirect and nothing cached: treat COMBO_URL as directly callable.
        target="$COMBO_URL"
        log "no redirect and no cache, calling COMBO_URL directly"
    fi
fi

# Rewrite whatever host the target names into loopback, so the restore does not
# depend on our own LAN address being what it was when the URL was written.
local_target=$(printf '%s' "$target" | sed -E "s#^https?://[^/]+#http://127.0.0.1:${API_PORT}#")

# 4. Fire it, with a few retries.
for attempt in 1 2 3 4 5; do
    code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' "$local_target" 2>/dev/null)
    if [ "$code" = "200" ]; then
        log "display restored (HTTP $code) on attempt $attempt"
        exit 0
    fi
    log "attempt $attempt returned HTTP ${code:-000}, retrying"
    sleep 10
done

log "FAILED to restore display after 5 attempts"
exit 1
