#!/bin/bash
# kiosk-video-drm — честный 1080p: остановить X, отдать экран mpv напрямую
# в DRM/KMS (zero-copy, без иксовых копирований кадров), после — вернуть X
# и киоск. Под X11 1080p на Pi 4 физически не тянется (~40 дропов/с, mpv ест
# 2.7 ядра на копиях), в DRM тот же ролик играет гладко.
#
# Запускается от пользователя киоска (он в группе video — DRM доступен);
# root нужен только на stop/start xinit — ровно две строки в sudoers.
#
# Использование: kiosk-video-drm.sh <url> <quality> <volume> <loop:0|1> <shuffle:0|1>

set -uo pipefail

URL="${1:?url required}"
QUALITY="${2:-1080}"
VOLUME="${3:-100}"
LOOP="${4:-1}"
SHUFFLE="${5:-1}"

LOG=/var/log/kiosk.log
log() {
    printf '%s %s\n' "$(date '+%F %T')" "video-drm: $*" >>"$LOG" 2>/dev/null
    logger -t kiosk-video-drm -- "$*" 2>/dev/null || true
}

# shellcheck disable=SC1091
[ -r /etc/kiosk/kiosk.env ] && . /etc/kiosk/kiosk.env

# Свежий yt-dlp живёт в venv приложения
VENV_BIN="$(dirname "$0")/.venv/bin"
[ -d "$VENV_BIN" ] || VENV_BIN="$HOME/kiosk/app/.venv/bin"
export PATH="$VENV_BIN:$PATH"

cleanup() {
    log "returning the screen to X and the kiosk"
    sudo systemctl start xinit.service 2>/dev/null
    sleep 4
    sudo systemctl restart --no-block kiosk-restore.service 2>/dev/null
}
trap cleanup EXIT

log "DRM video mode: quality<=${QUALITY} url=${URL}"
sudo systemctl stop xinit.service
sleep 1
# Пока yt-dlp разбирает ссылку, на экране была бы голая консоль с логином и
# IP — рисуем заставку (или хотя бы чёрный) прямо во фреймбуфер; пользователь
# в группе video, sudo не нужен.
SPLASH=/usr/local/share/kiosk/splash.jpg
for fb in /dev/fb*; do
    [ -w "$fb" ] || continue
    if [ -f "$SPLASH" ] && command -v ffmpeg >/dev/null; then
        ffmpeg -loglevel error -i "$SPLASH" -pix_fmt rgb565le -f fbdev "$fb" 2>/dev/null \
            && continue
    fi
    dd if=/dev/zero of="$fb" bs=1M count=32 2>/dev/null
done || true

ARGS=(
    --vo=gpu --gpu-context=drm
    # Аппаратный h264-декодер: в DRM он реально разгружает CPU (в X11 его
    # съедали копии кадров). 60fps-исходники на 1080p всё равно не тянутся —
    # лесенка: 1080p до 30fps, иначе честные 720p.
    --vd=lavc:h264_v4l2m2m,h264
    --fullscreen
    --input-ipc-server=/tmp/mpvsocket
    "--ytdl-format=bestvideo[height<=${QUALITY}][fps<=30][vcodec^=avc1]+bestaudio/bestvideo[height<=720][vcodec^=avc1]+bestaudio/best[height<=720]/best"
    "--volume=${VOLUME}"
    --no-terminal --really-quiet
    --network-timeout=30 --cache=yes --cache-secs=30
)
case "$URL" in
    *list=*) ARGS+=(--ytdl-raw-options=yes-playlist=) ;;
esac
[ "$LOOP" = "1" ] && case "$URL" in
    *list=*) ARGS+=(--loop-playlist=inf) ;;
    *)       ARGS+=(--loop-file=inf) ;;
esac

if [ "$SHUFFLE" = "1" ]; then
    case "$URL" in *list=*)
        ( for _ in $(seq 1 60); do sleep 2
              c=$(echo '{"command":["get_property","playlist-count"]}' \
                  | socat - /tmp/mpvsocket 2>/dev/null \
                  | grep -o '"data":[0-9]*' | cut -d: -f2)
              if [ "${c:-0}" -gt 1 ]; then
                  echo '{"command":["playlist-shuffle"]}' | socat - /tmp/mpvsocket
                  echo '{"command":["playlist-play-index",0]}' | socat - /tmp/mpvsocket
                  break
              fi
          done ) &
    esac
fi

mpv "${ARGS[@]}" "$URL" > /tmp/mpv_drm.log 2>&1
log "mpv exited ($?)"
