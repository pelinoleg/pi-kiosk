#!/bin/bash
# net-watchdog — keep a kiosk box on the LAN; reboot only as a last resort.
#
# One invocation = one check, driven by net-watchdog.timer. Escalation is
# counted in *consecutive* failed checks:
#   >= SOFT_FAIL    bounce the wireless interface
#   >= HARD_FAIL    restart NetworkManager
#   >= REBOOT_FAIL  reboot
# Any single successful check resets the counter to zero.
#
# Tunables live in /etc/kiosk/kiosk.env.

set -uo pipefail

# shellcheck disable=SC1091
[ -r /etc/kiosk/kiosk.env ] && . /etc/kiosk/kiosk.env

WIFI_IFACE="${WIFI_IFACE-wlan0}"
PROBE_HOSTS="${PROBE_HOSTS:-192.168.1.1}"
SOFT_FAIL="${SOFT_FAIL:-2}"
HARD_FAIL="${HARD_FAIL:-4}"
REBOOT_FAIL="${REBOOT_FAIL:-8}"
BOOT_GUARD="${BOOT_GUARD:-150}"
REBOOT_COOLDOWN="${REBOOT_COOLDOWN:-1800}"

STATE_DIR=/var/lib/kiosk
FAIL_FILE="$STATE_DIR/fail_count"
REBOOT_STAMP="$STATE_DIR/last_reboot"
LOG=/var/log/kiosk.log

log() {
    printf '%s %s\n' "$(date '+%F %T')" "net-watchdog: $*" >>"$LOG"
    logger -t net-watchdog -- "$*" 2>/dev/null || true
}

# Reachability of the LAN, not of the internet: what a kiosk displays usually
# lives on the local network, and the gateway answering proves the link is alive.
online() {
    local gw host
    gw=$(ip route show default 2>/dev/null | awk '/^default/{print $3; exit}')
    if [ -n "$gw" ] && ping -c 1 -W 2 "$gw" >/dev/null 2>&1; then
        return 0
    fi
    for host in $PROBE_HOSTS; do
        ping -c 1 -W 2 "$host" >/dev/null 2>&1 && return 0
    done
    return 1
}

bounce_iface() {
    [ -n "$WIFI_IFACE" ] || return 0
    log "soft recovery: bouncing $WIFI_IFACE"
    nmcli device disconnect "$WIFI_IFACE" >/dev/null 2>&1
    sleep 3
    nmcli device connect "$WIFI_IFACE" >/dev/null 2>&1
    sleep 8
}

restart_stack() {
    log "hard recovery: restarting NetworkManager"
    systemctl restart NetworkManager
    sleep 15
    [ -n "$WIFI_IFACE" ] && nmcli device connect "$WIFI_IFACE" >/dev/null 2>&1
    sleep 5
}

do_reboot() {
    local now last
    now=$(date +%s)
    last=$(cat "$REBOOT_STAMP" 2>/dev/null || echo 0)
    if [ $((now - last)) -lt "$REBOOT_COOLDOWN" ]; then
        log "reboot suppressed: last one was $((now - last))s ago (cooldown ${REBOOT_COOLDOWN}s)"
        return
    fi
    echo "$now" >"$REBOOT_STAMP"
    log "REBOOTING: network unrecoverable after $REBOOT_FAIL consecutive failed checks"
    sync
    systemctl reboot
}

mkdir -p "$STATE_DIR"

# Give the box a chance to finish booting before we start pulling levers.
[ "$(awk '{print int($1)}' /proc/uptime)" -lt "$BOOT_GUARD" ] && exit 0

if online; then
    [ "$(cat "$FAIL_FILE" 2>/dev/null || echo 0)" -gt 0 ] && log "network back up, resetting counter"
    echo 0 >"$FAIL_FILE"
    exit 0
fi

fails=$(( $(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" >"$FAIL_FILE"
log "check failed ($fails in a row)"

if   [ "$fails" -ge "$REBOOT_FAIL" ]; then do_reboot
elif [ "$fails" -ge "$HARD_FAIL"   ]; then restart_stack
elif [ "$fails" -ge "$SOFT_FAIL"   ]; then bounce_iface
fi

# Re-check immediately so a successful recovery clears the counter without
# waiting a further tick for the next timer firing.
if online; then
    log "recovered"
    echo 0 >"$FAIL_FILE"
fi
