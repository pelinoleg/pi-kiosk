#!/usr/bin/env bash
#
# pi-kiosk installer. Non-interactive by design: it is meant to be piped from
# curl, where stdin is the script itself and prompting is impossible.
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | bash
#
# Everything is configured through environment variables, all optional:
#
#   INSTALL_DIR   where the app lands              (default ~/kiosk)
#   KIOSK_USER    user that owns the session       (default the invoking user)
#   COMBO_URL     what to restore on screen        (default empty = disabled)
#   API_PORT      port for the control API         (default 7000)
#   WIFI_IFACE    interface the watchdog nurses    (default wlan0, empty = wired)
#   PROBE_HOSTS   LAN hosts that prove reachability (default the gateway only)
#   AUTOLOGIN=1   also configure console autologin (default off)
#   AIRPLAY       0 to skip the AirPlay receivers   (default 1)
#   AIRPLAY_NAME  name iOS shows                    (default the hostname)
#   HW_WATCHDOG   1 to arm the hardware watchdog    (default off, see README)
#   REMOTE        0 to skip the HAOBO remote control services (default on)
#   DISPLAY_OFF_AT / DISPLAY_ON_AT   HH:MM to blank the panel overnight
#
# HDMI is used automatically whenever a cable is present, and the built-in
# panel otherwise; both are never lit at once.
#   GITHUB_TOKEN  required only when the repo is private
#   KIOSK_REPO    owner/name to fetch from         (default below)
#   KIOSK_REF     branch or tag                    (default main)
#
# Re-running is safe: it upgrades in place and never overwrites an existing
# /etc/kiosk/kiosk.env.

set -euo pipefail

KIOSK_REPO="${KIOSK_REPO:-pelinoleg/pi-kiosk}"
KIOSK_REF="${KIOSK_REF:-main}"

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YEL=$'\033[1;33m'; NC=$'\033[0m'
info() { printf '%s[..]%s %s\n' "$GRN" "$NC" "$*"; }
warn() { printf '%s[!!]%s %s\n' "$YEL" "$NC" "$*"; }
die()  { printf '%s[XX]%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight --
[ "$(id -u)" -eq 0 ] && die "Run as your normal user, not root. sudo is used where needed."
command -v systemctl >/dev/null || die "This needs systemd."
command -v apt-get   >/dev/null || die "This targets Debian / Raspberry Pi OS (apt-get not found)."
sudo -n true 2>/dev/null || info "sudo will ask for your password."

KIOSK_USER="${KIOSK_USER:-$USER}"
KIOSK_HOME=$(getent passwd "$KIOSK_USER" | cut -d: -f6)
[ -n "$KIOSK_HOME" ] || die "Cannot resolve home directory for user '$KIOSK_USER'."
KIOSK_UID=$(id -u "$KIOSK_USER")
INSTALL_DIR="${INSTALL_DIR:-$KIOSK_HOME/kiosk}"
API_PORT="${API_PORT:-7000}"

info "user=$KIOSK_USER  dir=$INSTALL_DIR  port=$API_PORT"

# ------------------------------------------------------------------ sources --
# Either we are sitting in a checkout, or we fetch a tarball. A private repo
# needs a token; the API endpoint accepts one, raw.githubusercontent does too.
SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/app/main.py" ]; then
    SRC="$SELF_DIR"
    info "installing from local checkout: $SRC"
else
    command -v curl >/dev/null || die "curl is required to fetch the sources."
    SRC=$(mktemp -d)
    trap 'rm -rf "$SRC"' EXIT
    info "downloading $KIOSK_REPO@$KIOSK_REF"
    AUTH=()
    [ -n "${GITHUB_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN")
    if ! curl -fsSL "${AUTH[@]}" \
            "https://api.github.com/repos/$KIOSK_REPO/tarball/$KIOSK_REF" \
            -o "$SRC/src.tgz"; then
        die "Download failed. For a private repo, export GITHUB_TOKEN=<a token with repo scope>."
    fi
    tar xzf "$SRC/src.tgz" -C "$SRC" --strip-components=1
    rm -f "$SRC/src.tgz"
    [ -f "$SRC/app/main.py" ] || die "Downloaded archive does not look like pi-kiosk."
fi

# ------------------------------------------------------------------ packages --
info "installing system packages (this is the slow part)"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    xserver-xorg x11-xserver-utils xinit openbox unclutter \
    chromium \
    python3 python3-venv python3-pip python3-pyqt5 python3-dev \
    mpv ffmpeg mpg123 pulseaudio alsa-utils espeak-ng \
    pulseaudio-module-bluetooth \
    network-manager curl wget git socat scrot \
    >/dev/null || die "Package installation failed."
# python3-pyqt5: часы (clock.py) рисуются системным python — PyQt5 не в venv.
# scrot: эндпоинт /surface/screenshot. espeak-ng: офлайн-фоллбек tts-say.
# pulseaudio-module-bluetooth: Bluetooth-колонки (bt-* эндпоинты).
sudo systemctl enable --now bluetooth >/dev/null 2>&1 || true
sudo rfkill unblock bluetooth 2>/dev/null || true

# Raspberry Pi OS ships chromium; plain Debian calls it chromium-browser. Make
# sure at least one of them exists, and that /usr/bin/chromium resolves.
if ! command -v chromium >/dev/null; then
    sudo apt-get install -y -qq chromium-browser >/dev/null 2>&1 || true
    if command -v chromium-browser >/dev/null && [ ! -e /usr/bin/chromium ]; then
        sudo ln -sf "$(command -v chromium-browser)" /usr/bin/chromium
    fi
fi
command -v chromium >/dev/null || warn "No chromium binary found; browser tabs will not work."

# ------------------------------------------------------------------- payload --
info "installing application into $INSTALL_DIR"
sudo -u "$KIOSK_USER" mkdir -p "$INSTALL_DIR"

# INSTALL_DIR may legitimately sit inside the checkout - keeping the repo and
# the deployed app under one directory is easier to remember than two. In that
# case the payload is already in place and copying it would be cp complaining
# that source and destination are the same file, which under set -e aborts the
# whole install.
SRC_APP=$(cd "$SRC/app" && pwd)
DST_APP=$(cd "$INSTALL_DIR" && pwd)
if [ "$SRC_APP" != "$DST_APP" ]; then
    sudo cp "$SRC/app/main.py" "$SRC/app/clock.py" "$SRC/app/requirements.txt" \
            "$SRC/app/remote.html" "$SRC/app/remote-manifest.json" "$SRC/app/tts-voices.json" \
            "$SRC/app/remote-icon-192.png" "$SRC/app/remote-icon-512.png" "$INSTALL_DIR/"
    sudo cp -r "$SRC/app/remote" "$INSTALL_DIR/"
    [ -f "$SRC/app/notification.mp3" ] && sudo cp "$SRC/app/notification.mp3" "$INSTALL_DIR/"
else
    info "app already in place (installing into the checkout)"
fi
sudo cp "$SRC/scripts/start-surface-api.sh" "$INSTALL_DIR/"
sudo chown -R "$KIOSK_USER:$KIOSK_USER" "$INSTALL_DIR"
sudo chmod +x "$INSTALL_DIR/start-surface-api.sh"

info "creating the Python environment"
sudo -u "$KIOSK_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$KIOSK_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$KIOSK_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
    || die "pip install failed."

info "installing watchdog and restore scripts"
sudo install -m 0755 "$SRC/scripts/net-watchdog.sh"  /usr/local/bin/net-watchdog.sh
sudo install -m 0755 "$SRC/scripts/kiosk-restore.sh" /usr/local/bin/kiosk-restore.sh
sudo install -m 0755 "$SRC/scripts/kiosk-display.sh" /usr/local/bin/kiosk-display.sh
sudo install -m 0755 "$SRC/scripts/kiosk-output.sh"  /usr/local/bin/kiosk-output.sh
sudo install -m 0755 "$SRC/scripts/airplay-watch.sh" /usr/local/bin/airplay-watch.sh

# Output switching restarts surface-api, which needs root; grant exactly that.
sed "s|__USER__|$KIOSK_USER|g" "$SRC/config/sudoers-kiosk" | sudo tee /etc/sudoers.d/kiosk >/dev/null
sudo chmod 0440 /etc/sudoers.d/kiosk
sudo visudo -cf /etc/sudoers.d/kiosk >/dev/null 2>&1 || { sudo rm -f /etc/sudoers.d/kiosk; warn "sudoers snippet rejected, dropped it"; }
# Backlight files under /sys/class/backlight are owned by root:video, so the
# panel can be dimmed without sudo once the kiosk user is in that group.
sudo usermod -aG video "$KIOSK_USER" 2>/dev/null || true
sudo mkdir -p /var/lib/kiosk
sudo touch /var/log/kiosk.log
# Some of these run as root (watchdog, AirPlay watcher) and some as the session
# user (output picker, display control), and they share this state and log - so
# the user has to own them, or half the scripts fail on Permission denied while
# still exiting 0.
sudo chown "$KIOSK_USER:$KIOSK_USER" /var/lib/kiosk /var/log/kiosk.log
sudo chmod 0775 /var/lib/kiosk
sudo chmod 0664 /var/log/kiosk.log

# -------------------------------------------------------------------- config --
sudo mkdir -p /etc/kiosk
if [ -f /etc/kiosk/kiosk.env ]; then
    info "keeping existing /etc/kiosk/kiosk.env"
else
    info "writing /etc/kiosk/kiosk.env"
    GW=$(ip route show default 2>/dev/null | awk '/^default/{print $3; exit}')

    # Only nurse a wireless interface if we are actually on one. Defaulting to
    # wlan0 on a wired box would have the watchdog bouncing a down interface as
    # its first recovery step, which is noise at best.
    if [ -n "${WIFI_IFACE+set}" ]; then
        IFACE="$WIFI_IFACE"
    else
        DEV=$(ip route show default 2>/dev/null | awk '/^default/{print $5; exit}')
        case "$DEV" in
            wl*) IFACE="$DEV" ;;
            *)   IFACE="" ;;
        esac
    fi

    sudo cp "$SRC/config/kiosk.env.example" /etc/kiosk/kiosk.env
    sudo sed -i "s|^API_PORT=.*|API_PORT=${API_PORT}|" /etc/kiosk/kiosk.env
    sudo sed -i "s|^COMBO_URL=.*|COMBO_URL=${COMBO_URL:-}|" /etc/kiosk/kiosk.env
    sudo sed -i "s|^WIFI_IFACE=.*|WIFI_IFACE=${IFACE}|" /etc/kiosk/kiosk.env
    sudo sed -i "s|^PROBE_HOSTS=.*|PROBE_HOSTS=\"${PROBE_HOSTS:-$GW}\"|" /etc/kiosk/kiosk.env
    sudo sed -i "s|^DISPLAY_OFF_AT=.*|DISPLAY_OFF_AT=${DISPLAY_OFF_AT:-}|" /etc/kiosk/kiosk.env
    sudo sed -i "s|^DISPLAY_ON_AT=.*|DISPLAY_ON_AT=${DISPLAY_ON_AT:-}|" /etc/kiosk/kiosk.env
fi

info "installing the X session"
sudo -u "$KIOSK_USER" cp "$SRC/config/xinitrc" "$KIOSK_HOME/.xinitrc"
sudo chmod +x "$KIOSK_HOME/.xinitrc"

# Xorg.wrap ships with allowed_users=console, which means a user without a
# console session may not start X. A systemd service has no controlling TTY, so
# startx dies with "Only console users are allowed to run the X server" and the
# unit sits in an auto-restart loop forever. Widen it.
info "allowing X to start from a service (Xwrapper)"
sudo mkdir -p /etc/X11
sudo tee /etc/X11/Xwrapper.config >/dev/null <<'EOF'
# Managed by pi-kiosk: the kiosk starts X from systemd, which has no console
# session, so the stock allowed_users=console would refuse to run the server.
allowed_users=anybody
needs_root_rights=yes
EOF

# ------------------------------------------------------------------- systemd --
info "installing systemd units"
# Times for the panel schedule come from the environment or an existing config;
# systemd cannot read OnCalendar from an EnvironmentFile, so they are baked in.
DISPLAY_OFF_AT="${DISPLAY_OFF_AT-$(sed -n 's/^DISPLAY_OFF_AT=//p' /etc/kiosk/kiosk.env 2>/dev/null)}"
DISPLAY_ON_AT="${DISPLAY_ON_AT-$(sed -n 's/^DISPLAY_ON_AT=//p' /etc/kiosk/kiosk.env 2>/dev/null)}"

render() {
    sed -e "s|__USER__|$KIOSK_USER|g" \
        -e "s|__HOME__|$KIOSK_HOME|g" \
        -e "s|__UID__|$KIOSK_UID|g" \
        -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        -e "s|__DISPLAY_OFF_AT__|${DISPLAY_OFF_AT}|g" \
        -e "s|__DISPLAY_ON_AT__|${DISPLAY_ON_AT}|g" \
        "$1" | sudo tee "/etc/systemd/system/$(basename "$1")" >/dev/null
}
for u in "$SRC"/systemd/*.service "$SRC"/systemd/*.timer; do render "$u"; done

# An unset time would leave OnCalendar as a literal placeholder, which systemd
# rejects - so the schedule is only armed when both ends are actually set.
if [ -n "$DISPLAY_OFF_AT" ] && [ -n "$DISPLAY_ON_AT" ]; then
    info "panel schedule: off at $DISPLAY_OFF_AT, on at $DISPLAY_ON_AT"
    SCHEDULE=1
else
    sudo rm -f /etc/systemd/system/kiosk-display-off.timer \
               /etc/systemd/system/kiosk-display-on.timer
    SCHEDULE=0
fi

# A zero-length unit file is reported by systemd as "masked", with no error at
# all. That is exactly what an unclean shutdown mid-install leaves behind, so
# verify sizes rather than trusting that the writes landed.
for u in surface-api.service xinit.service net-watchdog.service net-watchdog.timer kiosk-restore.service; do
    [ -s "/etc/systemd/system/$u" ] || die "Unit $u came out empty. Disk full, or the box died mid-write."
done

info "wifi power save off, log rotation, hardware watchdog"
sudo mkdir -p /etc/NetworkManager/conf.d
printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/10-no-powersave.conf >/dev/null

sudo tee /etc/logrotate.d/kiosk >/dev/null <<'EOF'
/var/log/kiosk.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# The BCM watchdog tops out around 15s of hardware timeout. Asking for more
# than that made a Pi 4 reset itself every 30-90 seconds, with no panic and no
# trace in the logs - and on boxes with log2ram the evidence is wiped by the
# reset it caused. So this is opt-in, and clamped when it is on.
sudo mkdir -p /etc/systemd/system.conf.d
if [ "${HW_WATCHDOG:-0}" = "1" ]; then
    HW_WATCHDOG_SEC="${HW_WATCHDOG_SEC:-14}"
    [ "$HW_WATCHDOG_SEC" -gt 14 ] 2>/dev/null && HW_WATCHDOG_SEC=14
    info "hardware watchdog on, ${HW_WATCHDOG_SEC}s"
    sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null <<EOF
[Manager]
RuntimeWatchdogSec=${HW_WATCHDOG_SEC}
RebootWatchdogSec=2min
EOF
else
    sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null <<'EOF'
# Hardware watchdog left off: values above ~15s make the BCM chip reset the
# board spontaneously. Turn it on by reinstalling with HW_WATCHDOG=1.
[Manager]
RuntimeWatchdogSec=0
EOF
fi

# comitup manages NetworkManager itself and drops to an access point on any
# connectivity hiccup, which reads to a user as "it forgot my wifi". If it is
# installed, it will fight this watchdog, so stand it down.
if systemctl list-unit-files 2>/dev/null | grep -q '^comitup'; then
    warn "comitup found — disabling it, it conflicts with the watchdog"
    sudo systemctl disable --now comitup comitup-web 2>/dev/null || true
fi

# ------------------------------------------------------------------ airplay --
if [ "${AIRPLAY:-1}" = "1" ]; then
    info "installing AirPlay receivers (uxplay + shairport-sync)"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        uxplay shairport-sync avahi-daemon \
        gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-x \
        >/dev/null 2>&1 || warn "AirPlay packages failed to install"

    if command -v uxplay >/dev/null; then
        # Discovery is mDNS; without Avahi the device simply never appears on iOS.
        sudo systemctl enable --now avahi-daemon >/dev/null 2>&1
        # The packaged unit runs as its own user against ALSA and would fight us
        # for the sound device; ours runs as the kiosk user against PulseAudio.
        sudo systemctl disable --now shairport-sync >/dev/null 2>&1 || true

        AIRPLAY_NAME="${AIRPLAY_NAME:-$(hostname)}"
        if ! grep -q '^AIRPLAY_NAME=' /etc/kiosk/kiosk.env 2>/dev/null; then
            printf '\nAIRPLAY_NAME=%s\nAIRPLAY_VIDEO_PORT=%s\n' \
                "$AIRPLAY_NAME" "${AIRPLAY_VIDEO_PORT:-35000}" \
                | sudo tee -a /etc/kiosk/kiosk.env >/dev/null
        else
            sudo sed -i "s|^AIRPLAY_NAME=.*|AIRPLAY_NAME=${AIRPLAY_NAME}|" /etc/kiosk/kiosk.env
        fi
        AIRPLAY_INSTALLED=1
    else
        warn "uxplay not available, skipping AirPlay"
    fi
fi

# --------------------------------------------------------------------- ufw ----
# A firewall that silently drops the control port is easy to miss, because
# everything still answers on loopback while nothing answers from the network.
# ufw lives in /usr/sbin, which is not on a normal user's PATH, so `command -v`
# alone reports it missing on exactly the machines that have it.
UFW=$(command -v ufw || true)
for c in /usr/sbin/ufw /sbin/ufw; do
    [ -n "$UFW" ] && break
    [ -x "$c" ] && UFW="$c"
done

if [ -n "$UFW" ] && sudo "$UFW" status 2>/dev/null | grep -q '^Status: active'; then
    info "opening ports in ufw"
    sudo "$UFW" allow "${API_PORT}/tcp" >/dev/null 2>&1
    sudo "$UFW" allow 5353/udp >/dev/null 2>&1        # mDNS, for AirPlay discovery
    if [ "${REMOTE:-1}" = "1" ]; then
        sudo "$UFW" allow "${REMOTE_PORT:-5050}/tcp" >/dev/null 2>&1  # HAOBO remote UI
    fi
    if [ "${AIRPLAY_INSTALLED:-0}" = "1" ]; then
        P="${AIRPLAY_VIDEO_PORT:-35000}"
        sudo "$UFW" allow "${P}:$((P + 2))/tcp" >/dev/null 2>&1
        sudo "$UFW" allow "${P}:$((P + 2))/udp" >/dev/null 2>&1
        sudo "$UFW" allow 5000/tcp      >/dev/null 2>&1  # shairport-sync RTSP
        sudo "$UFW" allow 6001:6011/udp >/dev/null 2>&1  # shairport-sync audio/control
        sudo "$UFW" allow 7000/udp      >/dev/null 2>&1  # uxplay timing/control
    fi
    sudo "$UFW" reload >/dev/null 2>&1
fi

if [ "${AUTOLOGIN:-0}" = "1" ] && command -v raspi-config >/dev/null; then
    info "enabling console autologin"
    sudo raspi-config nonint do_boot_behaviour B2 || warn "autologin setup failed, continuing"
fi

info "enabling services"
sudo systemctl daemon-reload
sudo systemctl enable --now xinit.service        >/dev/null 2>&1 || warn "xinit failed to start"
sudo systemctl enable --now surface-api.service  >/dev/null 2>&1 || warn "surface-api failed to start"
sudo systemctl enable --now net-watchdog.timer   >/dev/null 2>&1 || warn "watchdog timer failed to start"
sudo systemctl enable kiosk-restore.service      >/dev/null 2>&1 || true
sudo systemctl enable --now kiosk-output.timer   >/dev/null 2>&1 || warn "output watcher failed to start"
sudo systemctl enable --now kiosk-pkg-update.timer >/dev/null 2>&1 || warn "pkg update timer failed to start"
if [ "${REMOTE:-1}" = "1" ]; then
    # Пульт HAOBO: конфиг с ключами живёт вне репозитория; на первой установке
    # кладём пример, дальше никогда не перетираем.
    [ -f "$INSTALL_DIR/remote/config.json" ] \
        || sudo -u "$KIOSK_USER" cp "$INSTALL_DIR/remote/config.example.json" "$INSTALL_DIR/remote/config.json"
    sudo systemctl enable --now remote-server.service   >/dev/null 2>&1 || warn "remote-server failed to start"
    sudo systemctl enable --now remote-listener.service >/dev/null 2>&1 || true
fi
if [ "${SCHEDULE:-0}" = "1" ]; then
    sudo systemctl enable --now kiosk-display-off.timer >/dev/null 2>&1 || warn "panel off timer failed"
    sudo systemctl enable --now kiosk-display-on.timer  >/dev/null 2>&1 || warn "panel on timer failed"
fi
if [ "${AIRPLAY_INSTALLED:-0}" = "1" ]; then
    sudo systemctl enable --now airplay-video.service >/dev/null 2>&1 || warn "airplay-video failed to start"
    sudo systemctl enable --now airplay-audio.service >/dev/null 2>&1 || warn "airplay-audio failed to start"
    # Without this the mirror window stays up after a phone disconnects and the
    # screen looks frozen on the last frame.
    sudo systemctl enable --now airplay-watch.timer >/dev/null 2>&1 || warn "airplay watcher failed to start"
fi

# Flush to disk before anything can hard-hang and truncate what we just wrote.
sync

# --------------------------------------------------------------------- check --
echo
info "verifying"
ok=1
CHECK_UNITS="xinit.service surface-api.service net-watchdog.timer"
[ "${AIRPLAY_INSTALLED:-0}" = "1" ] && CHECK_UNITS="$CHECK_UNITS airplay-video.service airplay-audio.service"
for u in $CHECK_UNITS; do
    state=$(systemctl is-active "$u" 2>&1 || true)
    printf '  %-24s %s\n' "$u" "$state"
    [ "$state" = active ] || ok=0
done

sleep 3
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${API_PORT}/" 2>/dev/null || echo 000)
printf '  %-24s HTTP %s\n' "API on :$API_PORT" "$code"
[ "$code" = 200 ] || ok=0

echo
if [ "$ok" = 1 ]; then
    info "Done. API: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${API_PORT}/"
else
    warn "Installed, but something is not up yet. Check:"
    warn "  systemctl status surface-api xinit --no-pager"
    warn "  tail -50 /var/log/kiosk.log"
fi
echo
info "Config:  /etc/kiosk/kiosk.env   (set COMBO_URL to enable display restore)"
info "Log:     /var/log/kiosk.log"
