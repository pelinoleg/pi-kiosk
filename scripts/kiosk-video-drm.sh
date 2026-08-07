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
    # kiosk-output вернёт правильную подсветку панели, если её гасили мы
    /usr/local/bin/kiosk-output.sh 2>/dev/null || true
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

# Играть на том же экране, что выбран в киоске: без явного коннектора mpv
# берёт первый попавшийся, и видео уезжало на мини-панель, подсвечивая обе.
ACTIVE=$(cat /var/lib/kiosk/active_output 2>/dev/null)
case "$ACTIVE" in
    HDMI-1) DRM_CONN="HDMI-A-1" ;;
    HDMI-2) DRM_CONN="HDMI-A-2" ;;
    "")     DRM_CONN="" ;;
    *)      DRM_CONN="$ACTIVE" ;;
esac

ARGS=(
    --vo=gpu --gpu-context=drm
    --fullscreen
    --input-ipc-server=/tmp/mpvsocket
    # Лесенка качества (замерено на живых роликах 2026-08-07):
    #  1) h264 >=720p до 30fps — гладко всегда (hw-декодер);
    #  2) AV1 до 720p — для HDR-роликов, у которых h264 обрывается на 480p:
    #     dav1d на 4 ядрах тянет 720p60 приемлемо (1080p60 — уже ~18 дроп/с);
    #  3) любой h264 до 720p; 4) что осталось.
    "--ytdl-format=bestvideo[height>=720][height<=${QUALITY}][fps<=30][vcodec^=avc1]+bestaudio/bestvideo[height>=600][height<=720][vcodec^=av01]+bestaudio/bestvideo[height<=720][vcodec^=avc1]+bestaudio/best[height<=720]/best"
    "--volume=${VOLUME}"
    # Не глушить вывод: с --really-quiet любая смерть mpv оставляла пустой
    # лог и никакой диагностики.
    --msg-level=all=warn
    --network-timeout=30 --cache=yes --cache-secs=30
)
[ -n "$DRM_CONN" ] && ARGS+=("--drm-connector=${DRM_CONN}")
# Когда видео идёт на HDMI, мини-панель светилась остатками фреймбуфера —
# гасим её подсветку на время ролика (kiosk-output вернёт после).
case "$DRM_CONN" in HDMI*)
    for bl in /sys/class/backlight/*/bl_power; do
        [ -w "$bl" ] && echo 4 > "$bl" 2>/dev/null
    done ;;
esac
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

START=$(date +%s)
mpv "${ARGS[@]}" "$URL" > /tmp/mpv_drm.log 2>&1
RC=$?
# Смерть в первые секунды — почти всегда гонка за DRM/VT при передаче
# экрана; одна повторная попытка лечит её молча.
if [ "$RC" -ne 0 ] && [ $(( $(date +%s) - START )) -lt 10 ]; then
    log "mpv died early (rc=$RC), retrying once"
    sleep 2
    mpv "${ARGS[@]}" "$URL" >> /tmp/mpv_drm.log 2>&1
    RC=$?
fi
log "mpv exited ($RC)"
