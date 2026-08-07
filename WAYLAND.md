# Ветка wayland — эксперимент по замене X11 на Wayland (labwc)

## Зачем

Под X11 видео упирается в CPU-копии кадров: 1080p любой ценой ~40 дропов/с
(даже с аппаратным декодером v4l2m2m — mpv ест 269% CPU). Обход сделан
DRM-видеорежимом (см. scripts/kiosk-video-drm.sh), но он останавливает X.
Wayland-композитор с dmabuf теоретически даёт zero-copy видео прямо в окне:
плавный 1080p без остановки сессии — плюс встроенный композитинг (часы
полупрозрачны без picom).

## Что уже выяснено (2026-08-07, pi4, живые замеры)

| Сценарий | Результат |
|---|---|
| X11, vo=xv, 720p60 | ~0-3 дропа/с — эталон текущего киоска |
| X11, vo=xv или gpu, 1080p60 | ~40 дропов/с, mpv 269% CPU — непригодно |
| DRM (без X), sw decode, 1080p60 | ~43 дропа/с — рендер тянет, декод нет |
| DRM, v4l2m2m hw decode, 480p30 | ~3 дропа/с |
| cage (Wayland) | segfault на старте, и с WLR_DRM_DEVICES тоже |
| labwc из systemd-run/openvt по SSH | компоситор без выходов: logind не даёт seat не-активной сессии; mpv: «No outputs found» |

Вывод дня: Wayland-замеры **невозможно честно снять из SSH-сессии** — нужен
настоящий автологин на VT (как делает реальная загрузка). Поэтому эксперимент
доводится на свежей установке этой веткой.

## План ветки

1. `wayland-session.service` вместо `xinit.service`: автологин-запуск labwc
   на tty1 (PAMName=login + TTYPath=/dev/tty1 + StandardInput=tty — как
   у getty), `WLR_DRM_DEVICES=/dev/dri/card1`.
2. `~/.config/labwc/autostart`: chromium `--ozone-platform=wayland --kiosk`,
   выбор выхода (wlr-randr вместо xrandr в kiosk-output.sh), unclutter не
   нужен (labwc умеет прятать курсор).
3. mpv в окне: `--vo=dmabuf-wayland` — снять дропы на /home/oleg/test1080.mp4
   (файл уже лежит на pi4; 1080p60 h264, 90 секунд).
4. Часы: та же PyQt поверх — под Wayland нужен qtwayland5; прозрачность
   бесплатно от композитора.
5. Скриншоты: scrot не работает — grim.
6. Если dmabuf-wayland даст ≤5 дропов/с на 1080p60 — мигрируем; если нет —
   остаёмся на X11 + DRM-видеорежим и закрываем вопрос.

## Как гонять замеры (когда сессия настоящая)

```bash
mpv --no-config --vo=dmabuf-wayland --vd=lavc:h264_v4l2m2m,h264 \
    --input-ipc-server=/tmp/mpvtest --mute=yes --fullscreen \
    --loop-file=inf /home/oleg/test1080.mp4 &
sleep 15
echo '{"command":["get_property","frame-drop-count"]}' | socat - /tmp/mpvtest
sleep 30
echo '{"command":["get_property","frame-drop-count"]}' | socat - /tmp/mpvtest
```
