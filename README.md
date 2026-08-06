# pi-kiosk

Киоск на Raspberry Pi: полноэкранный Chromium с ротацией вкладок, HTTP-API для
управления экраном, звуком и плеером, плюс сетевой watchdog, который сам чинит
отвалившийся Wi-Fi и восстанавливает картинку после перезагрузки.

Ставится одной командой на чистую систему.

---

## Что нужно

**Железо.** Raspberry Pi 3B и новее. На 3B всё работает, но Chromium с
несколькими вкладками грузит его под потолок — если собираешь заново, бери Pi 4
или 5.

**Питание.** Честные 5V/2.5A для 3B, 5V/3A для 4, официальный 27W для 5.
Дешёвая зарядка от телефона — самая частая причина отвалов Wi-Fi, которые
выглядят как проблемы с сетью.

**Система.** **Raspberry Pi OS (64-bit) Lite**, на базе Debian 12 bookworm или
13 trixie. Именно **Lite**, без рабочего стола — X, оконный менеджер и браузер
установщик ставит сам, ровно в том минимуме, который нужен киоску. Полноценная
версия с десктопом тоже заведётся, но притащит лишнее.

Обычный Debian 12/13 на другом железе подойдёт, если есть systemd и apt.
Требуется **NetworkManager** — в Raspberry Pi OS начиная с bookworm он стоит по
умолчанию.

**Сеть.** Лучше кабелем. Если Wi-Fi — см. раздел про грабли ниже.

При записи образа в Raspberry Pi Imager сразу задай имя пользователя, включи SSH
и пропиши Wi-Fi — тогда после первой загрузки останется только выполнить одну
команду.

---

## Установка

На свежей системе, одной командой:

```bash
curl -fsSL https://raw.githubusercontent.com/pelinoleg/pi-kiosk/main/install.sh | bash
```

Либо из клона, если хочется сначала посмотреть, что будет выполняться:

```bash
git clone https://github.com/pelinoleg/pi-kiosk && cd pi-kiosk && ./install.sh
```

Если репозиторий когда-нибудь станет приватным, установщику нужно будет
передать токен GitHub с правами `repo` — он это поддерживает:

```bash
export GITHUB_TOKEN=ghp_...
curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://raw.githubusercontent.com/pelinoleg/pi-kiosk/main/install.sh | bash
```

Установщик неинтерактивный — он не задаёт вопросов и не ждёт ввода, поэтому
безопасно работает в конвейере из `curl`. Повторный запуск обновляет установку
на месте и **не** перетирает уже существующий конфиг.

### Настройки при установке

Всё через переменные окружения, все необязательные:

```bash
curl -fsSL https://raw.githubusercontent.com/pelinoleg/pi-kiosk/main/install.sh \
  | COMBO_URL=http://192.168.1.95:8888/surface/combo AUTOLOGIN=1 bash
```

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `INSTALL_DIR` | `~/kiosk` | куда положить приложение |
| `KIOSK_USER` | текущий пользователь | под кем крутится сессия |
| `COMBO_URL` | пусто | что показывать после перезагрузки; пусто = восстановление выключено |
| `API_PORT` | `7000` | порт управляющего API |
| `WIFI_IFACE` | `wlan0` | интерфейс для watchdog; пусто, если проводная сеть |
| `PROBE_HOSTS` | шлюз | хосты, доказывающие живость сети |
| `AUTOLOGIN` | `0` | `1` — включить автологин в консоль |
| `GITHUB_TOKEN` | — | нужен только для приватного репозитория |

Поменять потом можно в `/etc/kiosk/kiosk.env`, затем
`sudo systemctl restart surface-api`.

---

## Что ставится

| Юнит | Что делает |
|---|---|
| `xinit.service` | поднимает X и Openbox |
| `surface-api.service` | управляющее API на порту 7000 |
| `net-watchdog.timer` | проверяет сеть раз в минуту |
| `kiosk-restore.service` | после загрузки возвращает картинку на экран |

Пакеты: `xserver-xorg`, `xinit`, `openbox`, `unclutter`, `chromium`, `mpv`,
`ffmpeg`, `mpg123`, `pulseaudio`, `alsa-utils`, `network-manager`, `python3-venv`.
Python-зависимости ставятся в изолированный `.venv` внутри `INSTALL_DIR`.

Логи обоих скриптов — `/var/log/kiosk.log`, ротация настроена.

### Watchdog

Проверка раз в минуту. Считаются **подряд идущие** неудачи:

| Порог | Действие |
|---|---|
| 2 | передёрнуть Wi-Fi интерфейс |
| 4 | перезапустить NetworkManager |
| 8 (~8 минут) | перезагрузить машину |

Любая успешная проверка обнуляет счётчик. Защита от циклической перезагрузки:
первые 150 секунд после старта watchdog не делает ничего, и повторная
перезагрузка не чаще раза в 30 минут. Пороги настраиваются в конфиге.

Дополнительно включается **аппаратный watchdog** (`RuntimeWatchdogSec=20`) — он
перезагрузит Pi, если ядро зависнет целиком и софтовый watchdog уже не выполнится.

Живость определяется по локальной сети, а не по интернету: сначала пингуется
шлюз, потом `PROBE_HOSTS`. Так киоск не уйдёт в перезагрузку из-за того, что у
провайдера что-то упало.

### Восстановление экрана

`COMBO_URL` — единственный источник правды о том, что должно быть на экране.
Ожидается, что он отдаёт редирект на вызов к API самого киоска. Скрипт идёт по
редиректу, подменяет хост на `127.0.0.1` и дёргает результат — поэтому
восстановление работает, даже если IP машины сменился. Разрешённый адрес
кешируется в `/var/lib/kiosk/last-surface.url`, так что если сервер с
`COMBO_URL` поднимется позже киоска, картинка всё равно вернётся.

---

## API

Порт 7000. Основное:

```
GET  /surface/chrome-tabs?urls=...&urls=...&times=600&times=60
GET  /surface/display-on            /surface/display-off      /surface/display-state
GET  /surface/kill-all              /surface/status           /surface/processes
GET  /surface/clock-show            /surface/clock-hide       /surface/clock-toggle
GET  /surface/system-volume/{0-100} /surface/toggle-mute      /surface/audio-outputs
GET  /surface/playback-toggle       /surface/playback-next    /surface/playback-status
GET  /surface/webcam                /surface/immich/album     /surface/reboot
POST /surface/custom-command        /surface/tts-play-upload
```

Ротация вкладок: `urls` и `times` — параллельные списки, `times` в секундах.

```bash
curl "http://<ip>:7000/surface/chrome-tabs?urls=http://a/&urls=http://b/&times=600&times=60"
```

---

## Грабли

Всё ниже — реально отловленные проблемы, а не гипотетические.

**comitup.** Демон провижининга Wi-Fi. Сам управляет NetworkManager и при любой
потере связи сносит подключение и поднимает свою точку доступа. Со стороны
выглядит как «Pi забыл сеть» и «не видит SSID» — в режиме AP интерфейс не
сканирует эфир. Установщик его отключает, если находит.

**Пустые файлы после жёсткого зависания.** Если Pi повис и не сбросил данные на
диск, ext4 оставляет свежезаписанные файлы **нулевой длины**. systemd показывает
пустой юнит как `masked` — без единой ошибки, юнит просто молча не работает.
Установщик после записи проверяет размеры и делает `sync`. Если что-то ведёт
себя странно — сначала `ls -la /etc/systemd/system/`, а не `systemctl status`.

**Кабель через Wi-Fi репитер.** Репитер в режиме роутера выдаёт адрес из своей
подсети, и на Pi появляются два интерфейса с двумя маршрутами по умолчанию.
Трафик начинает уходить не туда. Либо кабель напрямую в роутер, либо только
Wi-Fi — но не оба сразу в разных подсетях.

**Неверный пароль виден со стороны роутера.** Если роутер на OpenWrt:

```sh
logread | grep -i PSK-MISMATCH                   # неправильный ключ
logread | grep -i <mac-адрес-pi>                 # вся история подключений
logread | grep "disassociated" | grep -oE "STA [0-9a-f:]+" | sort | uniq -c | sort -rn
```

Сравнение числа отвалов между клиентами сразу показывает, виноват роутер или
одно устройство. `AP-STA-POSSIBLE-PSK-MISMATCH` — неверный пароль,
`excessive missing ACKs` — радиомодуль клиента перестал отвечать (питание,
драйвер, power save), а не слабый сигнал.

**`Only console users are allowed to run the X server`.** Пакет
`xserver-xorg-legacy` ставит в `/etc/X11/Xwrapper.config` значение
`allowed_users=console`, а у systemd-сервиса консольной сессии нет — `startx`
падает, и юнит вечно висит в `activating (auto-restart)`. Установщик прописывает
туда `allowed_users=anybody`. Симптом легко принять за проблему с драйвером или
монитором, хотя дело только в этой строке.

**Power save.** У brcmfmac на Pi он вызывает залипания. Установщик выключает
его через `/etc/NetworkManager/conf.d/10-no-powersave.conf`.

**python-multipart.** Без него эндпоинты с загрузкой файлов (`tts-play-upload`)
падают при импорте. Внесён в `requirements.txt`.

**Имя выхода HDMI** различается между моделями Pi и драйверами. `.xinitrc`
перебирает `HDMI-1`, `HDMI-2`, `HDMI-A-1`, `HDMI-A-2`.

---

## Диагностика

```bash
systemctl status surface-api xinit net-watchdog.timer --no-pager
tail -50 /var/log/kiosk.log
journalctl -u surface-api -n 50 --no-pager

curl -s localhost:7000/surface/status | head
sudo /usr/local/bin/net-watchdog.sh && echo "сеть в порядке"
sudo /usr/local/bin/kiosk-restore.sh          # вернуть картинку прямо сейчас

ls -la /etc/systemd/system/                    # проверить, что юниты не пустые
nmcli device wifi list                         # видит ли Pi сеть
cat /proc/net/wireless                         # уровень сигнала
vcgencmd get_throttled                         # 0x0 = с питанием всё хорошо
```

`vcgencmd get_throttled` стоит смотреть первым при любых странностях: ненулевое
значение означает просадку питания, и тогда чинить надо блок питания, а не софт.

## Удаление

```bash
sudo systemctl disable --now surface-api xinit net-watchdog.timer kiosk-restore
sudo rm -f /etc/systemd/system/{surface-api,xinit,net-watchdog,kiosk-restore}.service \
           /etc/systemd/system/net-watchdog.timer
sudo rm -f /usr/local/bin/{net-watchdog,kiosk-restore}.sh
sudo rm -rf /etc/kiosk /var/lib/kiosk
sudo systemctl daemon-reload
```
