#!/bin/bash
# kiosk-backup — собрать в один архив всё, чего НЕТ в публичном репозитории,
# но без чего свежая установка не станет «тем самым» киоском: ключи, конфиги,
# кеш озвучек, спаренный Bluetooth. Запускать перед переездом; на новой машине
# после install.sh распаковать: sudo tar xzf kiosk-private-backup.tgz -C /
#
# Архив содержит секреты — в git и куда-либо публично не класть.

set -euo pipefail

OUT="${1:-$HOME/kiosk-private-backup.tgz}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/kiosk/app}"

sudo tar czf "$OUT" \
    --ignore-failed-read \
    /etc/kiosk/kiosk.env \
    "$INSTALL_DIR/remote/config.json" \
    /var/lib/kiosk/tts.json \
    /var/lib/kiosk/quiet.json \
    /var/lib/kiosk/clock.json \
    /var/lib/kiosk/audio_sink \
    /var/lib/kiosk/bt_keepalive \
    /var/lib/kiosk/output_override \
    /var/lib/kiosk/tts-cache \
    /var/lib/bluetooth \
    2>/dev/null || true

sudo chown "$USER" "$OUT"
chmod 600 "$OUT"
echo "Backup: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Восстановление на новой машине (после install.sh):"
echo "  sudo tar xzf $(basename "$OUT") -C /"
echo "  sudo systemctl restart surface-api bluetooth remote-server"
