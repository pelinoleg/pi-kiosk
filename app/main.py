from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
import subprocess
import json
import os
import re
import time
from typing import List, Dict, Any
from pydantic import BaseModel
import socket
import psutil
import logging
import pathlib
import uvicorn
import random
import requests
import tempfile
import threading
import shlex
import hashlib
import base64


def _detect_display_output() -> str:
    """Name of the X output the kiosk draws on.

    Hardcoding HDMI-1 breaks on any machine whose panel is called something
    else - a DSI mini screen, or a second HDMI port - and the failure is quiet:
    `xrandr --output {DISPLAY_OUTPUT} --auto` just returns non-zero, so display-on/off
    report errors and any command chained after it with && never runs.
    Override with DISPLAY_OUTPUT if detection picks the wrong one.
    """
    forced = os.environ.get("DISPLAY_OUTPUT")
    if forced:
        return forced
    try:
        out = subprocess.run(
            "DISPLAY=:0 xrandr --query", shell=True,
            capture_output=True, text=True, timeout=5,
        ).stdout
        connected = [
            line.split()[0] for line in out.splitlines()
            if " connected" in line and not line.startswith(" ")
        ]
        # Prefer an output that already has a mode set, else the first connected.
        for line in out.splitlines():
            if " connected" in line and re.search(r"\d+x\d+\+\d+\+\d+", line):
                return line.split()[0]
        if connected:
            return connected[0]
    except Exception:
        pass
    return "HDMI-1"


DISPLAY_OUTPUT = _detect_display_output()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/tmp/surface_api.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("surface-api")

# Создаем приложение FastAPI
app = FastAPI(
    title="Surface Control API",
    description="API для управления отображением и аудио на Surface мини-компьютере",
    version="1.0.0"
)

# Добавляем CORS для разрешения запросов с любых источников
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Константы
MPV_SOCKET = "/tmp/mpvsocket"
MPV_TIMEOUT = 3  # секунды для таймаута команд MPV
# Без --window-size: в режиме --kiosk окно само занимает весь экран, а зашитые
# 1920x1080 промахиваются на любой другой панели (DSI — 800x480).
CHROME_KIOSK_CMD = "/usr/bin/chromium --kiosk --disable-infobars --no-first-run --no-sandbox"
DEFAULT_HTML_DIR = "/tmp/surface_control"
# Файлы, которые лежат рядом с main.py
CURRENT_DIR = pathlib.Path(__file__).parent.absolute()
NOTIFICATION_FILE = os.path.join(CURRENT_DIR, "notification.mp3")
REMOTE_HTML_FILE = os.path.join(CURRENT_DIR, "remote.html")

# Создаем директории, если не существуют
os.makedirs(DEFAULT_HTML_DIR, exist_ok=True)


########################
# Вспомогательные классы и функции
########################

class CustomCommandRequest(BaseModel):
    command: str


def spawn(cmd: str) -> subprocess.Popen:
    """Запускает команду в фоне, не дожидаясь завершения"""
    return subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Функция для выполнения команд и получения вывода
def run_command(cmd: str) -> Dict[str, Any]:
    """Запускает shell-команду и возвращает stdout, stderr и код выхода"""
    logger.info(f"Выполнение команды: {cmd}")
    try:
        process = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        return {
            "stdout": process.stdout,
            "stderr": process.stderr,
            "exit_code": process.returncode,
            "success": process.returncode == 0
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "Timeout expired",
            "exit_code": -1,
            "success": False
        }
    except Exception as e:
        logger.error(f"Ошибка при выполнении команды: {e}")
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "success": False
        }


# Функция для отправки команд в MPV через сокет
async def mpv_command(command, timeout=MPV_TIMEOUT):
    """Отправляет команду в MPV через сокет JSON IPC и возвращает ответ"""
    if not os.path.exists(MPV_SOCKET):
        return {"error": "MPV socket не найден", "success": False}

    try:
        # Формируем JSON-команду
        if isinstance(command, list):
            cmd_json = json.dumps({"command": command})
        else:
            cmd_json = command if isinstance(command, str) else json.dumps(command)

        # Создаем сокет и подключаемся
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(MPV_SOCKET)

        # Отправляем команду
        sock.sendall(cmd_json.encode() + b'\n')

        # Получаем ответ
        response = b""
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            response += chunk
            if b'\n' in chunk:  # Ответ завершается символом новой строки
                break

        sock.close()

        # Парсим ответ
        try:
            return json.loads(response.decode())
        except json.JSONDecodeError:
            return {"response": response.decode(), "success": True}

    except socket.timeout:
        return {"error": "Таймаут соединения с MPV", "success": False}
    except socket.error as e:
        return {"error": f"Ошибка сокета: {str(e)}", "success": False}
    except Exception as e:
        logger.error(f"Ошибка при отправке команды MPV: {e}")
        return {"error": str(e), "success": False}


def get_current_volume():
    """Текущая громкость Master в процентах, либо None, если её не удалось прочитать"""
    result = run_command("amixer -D pulse get Master | grep -o '[0-9]*%' | head -1 | tr -d '%'")
    value = result["stdout"].strip()
    if result["success"] and value:
        try:
            return int(value)
        except ValueError:
            pass
    return None


def kill_chrome_processes():
    """Завершение всех процессов Chrome"""
    cmd = """
    export DISPLAY=:0
    killall chrome chromium chrome-browser chromium-browser 2>/dev/null || true
    pkill -f chrome || true
    pkill -9 -f chrome || true
    sleep 1
    """
    result = run_command(cmd)
    logger.info(f"Завершение процессов Chrome: {result['success']}")
    return result


def kill_mpv_processes():
    """Завершение всех процессов MPV"""
    cmd = """
    export DISPLAY=:0
    killall mpv 2>/dev/null || true
    pkill -f mpv || true
    pkill -9 -f mpv || true
    rm -f /tmp/mpvsocket || true
    sleep 1
    """

    result = run_command(cmd)
    logger.info(f"Завершение процессов MPV: {result['success']}")
    return result


# Функция для получения информации о процессах
def get_processes():
    chrome_processes = []
    mpv_processes = []

    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
        try:
            # Информация о процессе
            proc_info = proc.info
            cmd = proc_info['cmdline']

            if not cmd:
                continue

            cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)

            # Добавляем Chrome/Chromium процессы
            if 'chrome' in cmd_str.lower() or 'chromium' in cmd_str.lower():
                chrome_processes.append({
                    'pid': proc_info['pid'],
                    'name': proc_info['name'],
                    'command': cmd_str,
                    'running_time': time.time() - proc_info['create_time']
                })

            # Добавляем MPV процессы
            elif 'mpv' in cmd_str.lower():
                mpv_processes.append({
                    'pid': proc_info['pid'],
                    'name': proc_info['name'],
                    'command': cmd_str,
                    'running_time': time.time() - proc_info['create_time']
                })

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    return {
        'chrome': chrome_processes,
        'mpv': mpv_processes
    }


def create_immich_playlist(ip: str, videos: List[dict]) -> str:
    """Создает M3U плейлист для видео из Immich"""
    playlist_content = '#EXTM3U\n'

    for video in videos:
        video_id = video.get("id")
        if not video_id:
            continue

        video_name = video.get("originalFileName", "Untitled Video")
        video_url = f"http://{ip}/api/assets/{video_id}/video/playback"

        playlist_content += f'#EXTINF:0,{video_name}\n'
        playlist_content += f'{video_url}\n'

    return playlist_content


def create_immich_script(ip: str, api_key: str) -> str:
    """Создает скрипт для запуска плейлиста Immich с MPV"""
    script = f"""#!/bin/bash
# Скрипт автоматически сгенерирован Surface API

# Включаем экран
export DISPLAY=:0
xset dpms force on
xset s off
xset s noblank
xset dpms 0 0 0
xrandr --output {DISPLAY_OUTPUT} --auto

# Останавливаем предыдущие процессы плеера
pkill -f vlc || echo "VLC не был запущен"
pkill -f mpv || echo "MPV не был запущен"
sleep 1

# Очищаем старый сокет, если он существует
if [ -e "{MPV_SOCKET}" ]; then
  rm -f "{MPV_SOCKET}"
  echo "Удален старый сокет MPV"
fi

# Запускаем MPV с правильными параметрами
echo "Запуск MPV с плейлистом Immich..."
mpv --fullscreen \\
    --force-window=yes \\
    --input-ipc-server={MPV_SOCKET} \\
    --profile=gpu-hq \\
    --hwdec=auto-safe \\
    --volume=100 \\
    --network-timeout=30 \\
    --http-header-fields="x-api-key: {api_key}" \\
    --no-terminal \\
    --really-quiet \\
    --input-default-bindings=yes \\
    --input-vo-keyboard=yes \\
    --no-audio-display \\
    --force-window-position \\
    /tmp/immich_playlist.m3u

exit_code=$?
echo "MPV завершился с кодом: $exit_code"
exit $exit_code
"""
    return script


def save_immich_files(playlist_content: str, script_content: str) -> None:
    """Сохраняет плейлист и скрипт запуска на диск"""
    try:
        # Сохраняем плейлист
        with open('/tmp/immich_playlist.m3u', 'w') as f:
            f.write(playlist_content)

        # Сохраняем скрипт запуска
        with open('/tmp/play_immich.sh', 'w') as f:
            f.write(script_content)

        # Делаем скрипт исполняемым
        os.chmod('/tmp/play_immich.sh', 0o755)

        logger.info("Файлы плейлиста и скрипта сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения файлов: {str(e)}")
        raise Exception(f"Ошибка сохранения файлов: {str(e)}")


########################
# API эндпоинты
########################

@app.get("/")
async def root():
    """Корневой эндпоинт с информацией об API"""
    # Список маршрутов собирается из самого приложения: рукописный перечень
    # здесь годами отставал от реальности.
    endpoints = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None)
        if not path.startswith("/surface") or not methods:
            continue
        doc = (route.endpoint.__doc__ or "").strip().splitlines()
        for method in sorted(m for m in methods if m != "HEAD"):
            endpoints[f"{method} {path}"] = doc[0] if doc else ""
    return {
        "name": "Surface Control API",
        "version": "1.0.0",
        "description": "API для управления отображением и аудио на Surface мини-компьютере",
        "endpoints": dict(sorted(endpoints.items(), key=lambda kv: kv[0].split(" ", 1)[1])),
    }


@app.get("/surface/status")
async def system_status():
    """Получение общего статуса системы"""
    try:
        # Состояние дисплея
        display_state_cmd = f"DISPLAY=:0 xrandr --query | grep '^{DISPLAY_OUTPUT}'"
        display_result = run_command(display_state_cmd)
        display_state = "on" if re.search(r"\d+x\d+\+\d+\+\d+", display_result["stdout"]) else "off"

        # Проверка MPV
        mpv_active = os.path.exists(MPV_SOCKET)
        mpv_status = "unknown"
        if mpv_active:
            mpv_result = await mpv_command(["get_property", "pause"])
            mpv_status = "paused" if mpv_result.get("data") else "playing"

        # Проверка Chrome
        chrome_processes = [p for p in psutil.process_iter() if "chrome" in p.name().lower()]
        chrome_active = len(chrome_processes) > 0

        # Громкость системы
        volume = get_current_volume() or 0

        # Аудио выход
        audio_output_cmd = "pactl get-default-sink"
        audio_output_result = run_command(audio_output_cmd)
        current_sink = audio_output_result["stdout"].strip() if audio_output_result["success"] else "unknown"

        return {
            "display": {
                "state": display_state
            },
            "audio": {
                "volume": volume,
                "output": current_sink,
                "muted": False  # Нужно добавить проверку
            },
            "playback": {
                "mpv_active": mpv_active,
                "mpv_status": mpv_status if mpv_active else "stopped",
                "chrome_active": chrome_active
            },
            "system": {
                "timestamp": time.time(),
                "uptime": int(time.time() - psutil.boot_time())
            }
        }
    except Exception as e:
        logger.error(f"Ошибка при получении статуса системы: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@app.get("/surface/processes")
async def get_system_processes():
    """Получение информации о запущенных процессах"""
    try:
        processes = get_processes()
        return {
            "processes": processes,
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Ошибка при получении информации о процессах: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@app.get("/surface/display-state")
async def get_display_state():
    """Получение текущего состояния дисплея"""
    display_cmd = f"DISPLAY=:0 xrandr --query | grep '^{DISPLAY_OUTPUT}'"
    result = run_command(display_cmd)

    if result["success"]:
        # Активный выход содержит геометрию вида 1920x1080+0+0; если её нет — экран выключен
        state = "on" if re.search(r"\d+x\d+\+\d+\+\d+", result["stdout"]) else "off"
        return {"state": state}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка получения состояния дисплея: {result['stderr']}")


# Флаг «экран выключен намеренно». Его уважает kiosk-output.sh: без флага
# сторож выходов на ближайшем тике (раз в 30с) молча включал панель обратно —
# ровно так выглядело «выключил экран, а через 10-20 секунд он сам зажёгся».
DISPLAY_OFF_FLAG = "/var/lib/kiosk/display_off"


def set_display_off_flag(off: bool):
    try:
        if off:
            with open(DISPLAY_OFF_FLAG, "w") as f:
                f.write(f"{int(time.time())}\n")
        elif os.path.exists(DISPLAY_OFF_FLAG):
            os.remove(DISPLAY_OFF_FLAG)
    except OSError as e:
        logger.warning(f"Не удалось обновить {DISPLAY_OFF_FLAG}: {e}")


########################
# Режим тишины
########################
#
# В заданные окна времени (свои для каждого дня недели) киоск глух и тёмен:
# всё, что включает экран или издаёт звук, отвечает 409; airplay-watch
# игнорирует сессии; фоновый страж гасит экран, если тот как-то включился,
# и сам возвращает киоск, когда окно заканчивается. Выключить режим можно
# только явно — конфигом (страница /surface/remote) — больше ничем.

QUIET_CONFIG_FILE = "/var/lib/kiosk/quiet.json"
QUIET_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_QUIET_MARK = "quiet"  # содержимое display_off-флага, когда экран погасил страж


class QuietDay(BaseModel):
    enabled: bool = True
    start: str  # "HH:MM"
    end: str    # "HH:MM"; end < start означает окно через полночь


class QuietConfig(BaseModel):
    enabled: bool = False
    days: Dict[str, QuietDay] = {}


def load_quiet_config() -> dict:
    try:
        with open(QUIET_CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"enabled": False, "days": {}}


def _hhmm_to_minutes(value):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or ""))
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def _quiet_window(cfg: dict, day_index: int):
    day = cfg.get("days", {}).get(QUIET_DAYS[day_index])
    if not day or not day.get("enabled", True):
        return None
    start, end = _hhmm_to_minutes(day.get("start")), _hhmm_to_minutes(day.get("end"))
    if start is None or end is None or start == end:
        return None
    return start, end


def _schedule_quiet_now(cfg: dict) -> bool:
    """Тихо ли сейчас по расписанию (без учёта ручных переопределений)"""
    if not cfg.get("enabled"):
        return False
    now = time.localtime()
    minutes = now.tm_hour * 60 + now.tm_min
    today = _quiet_window(cfg, now.tm_wday)
    if today:
        start, end = today
        if start < end:
            if start <= minutes < end:
                return True
        elif minutes >= start:  # окно через полночь, вечерняя половина
            return True
    yesterday = _quiet_window(cfg, (now.tm_wday - 1) % 7)
    if yesterday:
        start, end = yesterday
        if start > end and minutes < end:  # утренняя половина вчерашнего окна
            return True
    return False


def _active_window_end_epoch(cfg: dict):
    """Момент окончания текущего тихого окна (epoch), если оно активно"""
    now = time.localtime()
    minutes = now.tm_hour * 60 + now.tm_min
    midnight = time.mktime(now[:3] + (0, 0, 0) + now[6:])
    today = _quiet_window(cfg, now.tm_wday)
    if today:
        start, end = today
        if start < end and start <= minutes < end:
            return midnight + end * 60
        if start > end and minutes >= start:
            return midnight + 86400 + end * 60
    yesterday = _quiet_window(cfg, (now.tm_wday - 1) % 7)
    if yesterday:
        start, end = yesterday
        if start > end and minutes < end:
            return midnight + end * 60
    return None


def quiet_now(cfg: dict = None) -> bool:
    """Активно ли сейчас тихое окно, с учётом ручных переопределений.

    override в конфиге: {"mode": "quiet"} — тихо прямо сейчас, до отмены,
    даже вне расписания; {"mode": "awake", "until": epoch} — пауза тишины
    до конца текущего окна, даже внутри расписания.
    """
    cfg = cfg if cfg is not None else load_quiet_config()
    override = cfg.get("override") or {}
    if override.get("mode") == "quiet":
        return True
    if override.get("mode") == "awake" and time.time() < override.get("until", 0):
        return False
    return _schedule_quiet_now(cfg)


def _save_quiet_config(cfg: dict):
    with open(QUIET_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _darken_now():
    """Погасить экран и звук немедленно, пометив это тишиной.

    Тишина — это не только тёмный экран: играющий MPV продолжал бы орать в
    колонку, а вкладка браузера может зашуметь когда угодно. Поэтому плеер
    останавливается, а системный звук глушится целиком.
    """
    try:
        with open(DISPLAY_OFF_FLAG, "w") as f:
            f.write(_QUIET_MARK + "\n")
    except OSError:
        pass
    run_command("pactl set-sink-mute @DEFAULT_SINK@ 1")
    kill_mpv_processes()
    run_command(f"export DISPLAY=:0 && xrandr --output {DISPLAY_OUTPUT} --off")


def _wake_and_restore():
    """Включить экран и звук немедленно и вернуть киоск"""
    set_display_off_flag(False)
    run_command("pactl set-sink-mute @DEFAULT_SINK@ 0")
    run_command(f"export DISPLAY=:0 && xset s off && xset s noblank "
                f"&& xrandr --output {DISPLAY_OUTPUT} --auto")
    _airplay_restore_kiosk()


def ensure_not_quiet():
    """Общий страж эндпоинтов, которым в тихое окно нельзя"""
    if quiet_now():
        raise HTTPException(
            status_code=409,
            detail="Режим тишины: всё, что включает экран или звук, игнорируется. "
                   "Отключается на странице /surface/remote",
        )


@app.get("/surface/quiet-state")
async def quiet_state():
    """Состояние режима тишины: конфиг, переопределение и тихо ли сейчас"""
    cfg = load_quiet_config()
    override = cfg.get("override") or {}
    if override.get("mode") == "awake" and time.time() >= override.get("until", 0):
        override = {}  # просроченная пауза — не показывать
    return {"enabled": cfg.get("enabled", False), "quiet_now": quiet_now(cfg),
            "schedule_quiet_now": _schedule_quiet_now(cfg),
            "override": override or None, "days": cfg.get("days", {})}


@app.post("/surface/quiet-config")
async def quiet_config(cfg: QuietConfig):
    """Сохранение конфига режима тишины (сбрасывает ручные переопределения)"""
    for name, day in cfg.days.items():
        if name not in QUIET_DAYS:
            raise HTTPException(status_code=400, detail=f"Неизвестный день: {name}")
        if _hhmm_to_minutes(day.start) is None or _hhmm_to_minutes(day.end) is None:
            raise HTTPException(status_code=400, detail=f"{name}: время должно быть HH:MM")
    data = cfg.dict()
    try:
        _save_quiet_config(data)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить конфиг: {e}")
    logger.info(f"Режим тишины: конфиг обновлён, enabled={data['enabled']}")
    return {"message": "Сохранено", "enabled": data["enabled"], "quiet_now": quiet_now(data),
            "days": data["days"]}


@app.get("/surface/quiet-force-on")
async def quiet_force_on():
    """Тихо прямо сейчас: экран гаснет немедленно, до явной отмены"""
    cfg = load_quiet_config()
    cfg["override"] = {"mode": "quiet", "since": int(time.time())}
    try:
        _save_quiet_config(cfg)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить: {e}")
    _darken_now()
    logger.info("Режим тишины: включён вручную, экран погашен")
    return {"message": "Тишина: экран погашен, всё игнорируется до отмены",
            "quiet_now": True}


@app.get("/surface/quiet-force-off")
async def quiet_force_off():
    """Отменить тишину сейчас: экран и киоск возвращаются немедленно.

    Внутри расписанного окна это пауза до конца окна — ночью тишина
    вернётся сама. Вне расписания просто снимает ручную тишину.
    """
    cfg = load_quiet_config()
    cfg["override"] = None
    if _schedule_quiet_now(cfg):
        until = _active_window_end_epoch(cfg) or (time.time() + 3600)
        cfg["override"] = {"mode": "awake", "until": int(until)}
    try:
        _save_quiet_config(cfg)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить: {e}")
    _wake_and_restore()
    logger.info("Режим тишины: отменён вручную, экран возвращается")
    return {"message": "Тишина снята, киоск возвращается на экран",
            "quiet_now": False,
            "paused_until": cfg["override"]["until"] if cfg.get("override") else None}


def _display_is_on() -> bool:
    result = subprocess.run(
        f"DISPLAY=:0 xrandr --query | grep '^{DISPLAY_OUTPUT}'",
        shell=True, capture_output=True, text=True, timeout=10,
    )
    return bool(re.search(r"\d+x\d+\+\d+\+\d+", result.stdout))


def _quiet_enforcer():
    """Фоновый страж: в тихое окно держит экран тёмным, после окна
    возвращает киоск — но только если гасил его сам (метка в флаге)."""
    while True:
        try:
            if quiet_now():
                if _display_is_on():
                    _darken_now()
                    logger.info("Режим тишины: экран и звук выключены")
            else:
                try:
                    mark = open(DISPLAY_OFF_FLAG).read().strip()
                except OSError:
                    mark = None
                if mark == _QUIET_MARK:
                    _wake_and_restore()
                    logger.info("Режим тишины закончился: экран включён, киоск восстанавливается")
        except Exception as e:
            logger.error(f"Страж режима тишины: {e}")
        time.sleep(30)


@app.on_event("startup")
async def _start_quiet_enforcer():
    threading.Thread(target=_quiet_enforcer, daemon=True).start()


@app.get("/surface/display-on")
async def turn_display_on():
    """Включение дисплея"""
    ensure_not_quiet()
    set_display_off_flag(False)
    cmd = f"export DISPLAY=:0 && xset s off && xset s noblank && xrandr --output {DISPLAY_OUTPUT} --auto"
    result = run_command(cmd)

    if result["success"]:
        return {"state": "on", "message": "Дисплей включен"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка включения дисплея: {result['stderr']}")


@app.get("/surface/display-off")
async def turn_display_off():
    """Выключение дисплея"""
    # Флаг ставится до xrandr, чтобы сторож выходов не успел вклиниться между
    # гашением и записью флага; при неудаче — снимается.
    set_display_off_flag(True)
    # Гаснущий экран с играющим видео — это не «выключил», а «выключил
    # картинку, звук орёт дальше». Плеер останавливается вместе с экраном.
    if run_command("pgrep -x mpv")["success"]:
        kill_mpv_processes()
    cmd = f"export DISPLAY=:0 && xrandr --output {DISPLAY_OUTPUT} --off"
    result = run_command(cmd)

    if result["success"]:
        return {"state": "off", "message": "Дисплей выключен"}
    else:
        set_display_off_flag(False)
        raise HTTPException(status_code=500, detail=f"Ошибка выключения дисплея: {result['stderr']}")


@app.get("/surface/playback-status")
def get_playback_status():
    """Проверка статуса MPV, включая паузу, с защитой от зависаний"""

    try:
        # 1. Сначала проверяем существование процесса (очень быстро)
        cmd = "timeout 0.2 pgrep -x mpv | wc -l"
        proc = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=0.3,
            text=True
        )

        count = 0
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                count = int(proc.stdout.strip())
            except:
                pass

        # Если MPV не запущен, сразу возвращаем результат
        if count == 0:
            return {
                "active": False,
                "status": "stopped",
                "socket_exists": os.path.exists("/tmp/mpvsocket")
            }

        # 2. MPV запущен, проверяем статус паузы через сокет с жестким таймаутом
        # Используем socat для безопасного взаимодействия с сокетом
        pause_cmd = "timeout 0.5 echo '{\"command\":[\"get_property\", \"pause\"]}' | socat - /tmp/mpvsocket 2>/dev/null || echo '{\"data\": false}'"

        pause_proc = subprocess.run(
            pause_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=0.7,  # Еще один уровень защиты
            text=True
        )

        # Определяем статус паузы
        is_paused = False
        status = "playing"

        # Пытаемся разобрать JSON-ответ от MPV
        try:
            pause_output = pause_proc.stdout.strip()
            if pause_output and "{" in pause_output:
                pause_data = json.loads(pause_output)
                is_paused = pause_data.get("data", False)
                status = "paused" if is_paused else "playing"
        except:
            # При ошибке парсинга предполагаем, что воспроизведение идет
            status = "playing"

        # Возвращаем результат
        return {
            "active": True,
            "status": status,
            "paused": is_paused,
            "socket_exists": os.path.exists("/tmp/mpvsocket")
        }

    except subprocess.TimeoutExpired:
        # Если произошел таймаут, возвращаем частичный результат
        return {
            "active": count > 0 if 'count' in locals() else False,
            "status": "timeout",
            "socket_exists": os.path.exists("/tmp/mpvsocket")
        }
    except Exception as e:
        # При любой ошибке возвращаем безопасный ответ
        return {
            "active": False,
            "status": "error",
            "error": str(e)
        }


@app.get("/surface/audio-outputs")
async def get_audio_outputs():
    """Получение списка аудио выходов"""
    # Получаем список всех аудио выходов
    cmd = "pactl list sinks | grep -E 'Sink #|Name:|Description:'"
    result = run_command(cmd)

    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"Ошибка получения аудио выходов: {result['stderr']}")

    # Получаем текущий активный выход
    current_cmd = "pactl get-default-sink"
    current_result = run_command(current_cmd)
    current_sink = current_result["stdout"].strip() if current_result["success"] else ""

    # Парсим вывод pactl
    outputs = []
    current_output = None

    for line in result["stdout"].split('\n'):
        line = line.strip()

        if line.startswith('Sink #'):
            if current_output:
                outputs.append(current_output)
            sink_id = line.split('#')[1].strip()
            current_output = {"id": sink_id, "active": False}

        elif line.startswith('Name:') and current_output:
            name = line.split(':', 1)[1].strip()
            current_output["name"] = name
            if name == current_sink:
                current_output["active"] = True

        elif line.startswith('Description:') and current_output:
            description = line.split(':', 1)[1].strip()
            current_output["description"] = description

    # Добавляем последний выход, если он есть
    if current_output:
        outputs.append(current_output)

    return {"outputs": outputs, "default_sink": current_sink}


@app.get("/surface/system-volume")
async def get_system_volume():
    """Получение текущей громкости системы"""
    volume = get_current_volume()
    if volume is None:
        raise HTTPException(status_code=500, detail="Не удалось прочитать громкость")
    return {"volume": volume}


@app.get("/surface/system-volume/{volume}")
async def set_system_volume(volume: int):
    """Установка громкости системы через GET-запрос для удобства"""
    if volume < 0:
        volume = 0
    elif volume > 150:
        volume = 150

    cmd = f"amixer -D pulse sset Master {volume}%"
    result = run_command(cmd)

    if result["success"]:
        return {"volume": volume, "message": f"Громкость установлена на {volume}%"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка установки громкости: {result['stderr']}")


@app.post("/surface/system-volume")
async def set_system_volume_post(volume: int = Query(..., ge=0, le=150)):
    """Установка громкости системы"""
    cmd = f"amixer -D pulse sset Master {volume}%"
    result = run_command(cmd)

    if result["success"]:
        return {"volume": volume, "message": f"Громкость установлена на {volume}%"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка установки громкости: {result['stderr']}")


@app.get("/surface/system-volume-up")
async def system_volume_up():
    """Увеличение громкости системы на 5%"""
    cmd = "amixer -D pulse sset Master 5%+"
    result = run_command(cmd)

    if result["success"]:
        volume = get_current_volume()
        if volume is not None:
            return {"volume": volume, "message": f"Громкость увеличена до {volume}%"}
        return {"success": True, "message": "Громкость увеличена на 5%"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка увеличения громкости: {result['stderr']}")


@app.get("/surface/system-volume-down")
async def system_volume_down():
    """Уменьшение громкости системы на 5%"""
    cmd = "amixer -D pulse sset Master 5%-"
    result = run_command(cmd)

    if result["success"]:
        volume = get_current_volume()
        if volume is not None:
            return {"volume": volume, "message": f"Громкость уменьшена до {volume}%"}
        return {"success": True, "message": "Громкость уменьшена на 5%"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка уменьшения громкости: {result['stderr']}")


AUDIO_SINK_FILE = "/var/lib/kiosk/audio_sink"


def set_default_sink(sink_id: str):
    """Сделать выход дефолтным, перегнать играющие потоки, запомнить выбор"""
    result = run_command(f"pactl set-default-sink {shlex.quote(sink_id)}")
    if not result["success"]:
        raise RuntimeError(result["stderr"].strip() or "pactl set-default-sink failed")
    # Уже играющие потоки сами не переезжают — перегнать их на новый выход.
    run_command(
        "pactl list short sink-inputs | cut -f1 | "
        f"xargs -r -I{{}} pactl move-sink-input {{}} {shlex.quote(sink_id)}"
    )
    # Запомнить выбор: start-surface-api.sh применяет его при каждом старте,
    # чтобы воткнутый позже HDMI не перетягивал звук на себя.
    try:
        with open(AUDIO_SINK_FILE, "w") as f:
            f.write(sink_id + "\n")
    except OSError as e:
        logger.warning(f"Не удалось запомнить аудиовыход: {e}")


@app.get("/surface/set-audio-output/{sink_id}")
async def set_audio_output(sink_id: str):
    """Переключение аудио выхода; выбор запоминается и переживает перезагрузку"""
    try:
        set_default_sink(sink_id)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка изменения аудио выхода: {e}")
    return {"sink_id": sink_id, "message": f"Аудио выход изменен на {sink_id}"}


########################
# Bluetooth-колонки
########################

_BT_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _bt_check_mac(mac: str):
    if not _BT_MAC_RE.fullmatch(mac):
        raise HTTPException(status_code=400, detail=f"Это не MAC-адрес: {mac}")


def _bt_device_list():
    devices = []
    listing = run_command("bluetoothctl devices")
    for line in listing["stdout"].splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3 or parts[0] != "Device":
            continue
        mac, name = parts[1], parts[2]
        # Безымянные устройства показываются как их же MAC с дефисами — это
        # эфирный шум (телефоны со случайными адресами), в списке ему не место.
        if name.replace("-", ":").upper() == mac.upper():
            continue
        info = run_command(f"bluetoothctl info {mac}")["stdout"]
        devices.append({
            "mac": mac,
            "name": name,
            "paired": "Paired: yes" in info,
            "connected": "Connected: yes" in info,
            # у колонок и наушников иконка audio-*
            "audio": "Icon: audio" in info,
        })
    # Колонки и знакомые устройства — наверх
    devices.sort(key=lambda d: (not d["connected"], not d["audio"], not d["paired"], d["name"].lower()))
    return devices


@app.get("/surface/bt-devices")
async def bt_devices():
    """Известные Bluetooth-устройства и их состояние"""
    return {"devices": _bt_device_list()}


@app.get("/surface/bt-scan")
async def bt_scan(seconds: int = Query(12, ge=3, le=30)):
    """Поиск Bluetooth-устройств рядом (блокирует на время поиска).

    Колонку перед поиском перевести в режим сопряжения.
    """
    # Контроллер может быть выключен (свежая загрузка, rfkill) — включаем сами.
    run_command("rfkill unblock bluetooth; bluetoothctl power on")
    run_command(f"timeout {seconds + 2} bluetoothctl --timeout {seconds} scan on")
    return {"devices": _bt_device_list(), "scanned_seconds": seconds}


@app.get("/surface/bt-connect/{mac}")
async def bt_connect(mac: str):
    """Подключить Bluetooth-колонку и сразу перевести звук на неё"""
    _bt_check_mac(mac)
    run_command("rfkill unblock bluetooth; bluetoothctl power on")
    # pair может ответить AlreadyExists — это не ошибка
    run_command(f"timeout 25 bluetoothctl pair {mac}")
    run_command(f"bluetoothctl trust {mac}")
    result = run_command(f"timeout 20 bluetoothctl connect {mac}")
    if "Connection successful" not in result["stdout"] and "already connected" not in result["stdout"].lower():
        raise HTTPException(status_code=502,
                            detail=f"Не подключилась: {result['stdout'].strip()[-200:] or result['stderr'].strip()[-200:]}")
    # Ждём, пока PulseAudio создаст sink, и делаем его выходом по умолчанию.
    sink_tag = mac.replace(":", "_")
    sink_name = None
    for _ in range(10):
        time.sleep(1)
        sinks = run_command("pactl list short sinks")["stdout"]
        for line in sinks.splitlines():
            if sink_tag in line:
                sink_name = line.split("\t")[1]
                break
        if sink_name:
            break
    if sink_name:
        try:
            set_default_sink(sink_name)
        except RuntimeError as e:
            logger.warning(f"Колонка подключена, но не стала дефолтом: {e}")
    return {"message": "Колонка подключена" + (", звук идёт на неё" if sink_name else
                       ", но аудиовыход не появился — проверь, что это колонка"),
            "mac": mac, "sink": sink_name}


@app.get("/surface/bt-disconnect/{mac}")
async def bt_disconnect(mac: str):
    """Отключить Bluetooth-устройство (остаётся сопряжённым)"""
    _bt_check_mac(mac)
    run_command(f"timeout 15 bluetoothctl disconnect {mac}")
    return {"message": "Отключено", "mac": mac}


@app.get("/surface/bt-forget/{mac}")
async def bt_forget(mac: str):
    """Забыть Bluetooth-устройство совсем"""
    _bt_check_mac(mac)
    run_command(f"bluetoothctl remove {mac}")
    return {"message": "Устройство забыто", "mac": mac}


@app.get("/surface/toggle-mute")
async def toggle_mute():
    """Включение/выключение звука"""
    cmd = "pactl set-sink-mute @DEFAULT_SINK@ toggle"
    result = run_command(cmd)

    if result["success"]:
        # Определяем, включен звук или выключен
        mute_cmd = "pactl list sinks | grep Mute | head -1"
        mute_result = run_command(mute_cmd)

        if mute_result["success"]:
            muted = "yes" in mute_result["stdout"].lower()
            return {"muted": muted, "message": f"Звук {'выключен' if muted else 'включен'}"}
        else:
            return {"success": True, "message": "Статус звука переключен"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка переключения звука: {result['stderr']}")


@app.get("/surface/playback-play")
async def playback_play():
    """Запуск воспроизведения MPV"""
    if not os.path.exists(MPV_SOCKET):
        raise HTTPException(status_code=404, detail="MPV не запущен")

    result = await mpv_command(["set_property", "pause", False])

    if result.get("error"):
        raise HTTPException(status_code=500, detail=f"Ошибка запуска воспроизведения: {result['error']}")

    return {"status": "playing", "message": "Воспроизведение запущено"}


@app.get("/surface/playback-pause")
async def playback_pause():
    """Приостановка воспроизведения MPV"""
    if not os.path.exists(MPV_SOCKET):
        raise HTTPException(status_code=404, detail="MPV не запущен")

    result = await mpv_command(["set_property", "pause", True])

    if result.get("error"):
        raise HTTPException(status_code=500, detail=f"Ошибка приостановки воспроизведения: {result['error']}")

    return {"status": "paused", "message": "Воспроизведение приостановлено"}


@app.get("/surface/playback-toggle")
async def playback_toggle():
    """Переключение паузы/воспроизведения MPV"""
    if not os.path.exists(MPV_SOCKET):
        raise HTTPException(status_code=404, detail="MPV не запущен")

    result = await mpv_command(["cycle", "pause"])

    if result.get("error"):
        raise HTTPException(status_code=500, detail=f"Ошибка переключения воспроизведения: {result['error']}")

    # Получаем новый статус
    pause_status = await mpv_command(["get_property", "pause"])
    status = "paused" if pause_status.get("data") else "playing"

    return {"status": status, "message": f"Воспроизведение {'приостановлено' if status == 'paused' else 'запущено'}"}


@app.get("/surface/playback-next")
async def playback_next():
    """Переход к следующему треку в плейлисте MPV"""
    if not os.path.exists(MPV_SOCKET):
        raise HTTPException(status_code=404, detail="MPV не запущен")

    result = await mpv_command(["playlist-next"])

    if result.get("error"):
        raise HTTPException(status_code=500, detail=f"Ошибка перехода к следующему треку: {result['error']}")

    return {"message": "Переход к следующему треку выполнен"}


@app.get("/surface/playback-prev")
async def playback_prev():
    """Переход к предыдущему треку в плейлисте MPV"""
    if not os.path.exists(MPV_SOCKET):
        raise HTTPException(status_code=404, detail="MPV не запущен")

    result = await mpv_command(["playlist-prev"])

    if result.get("error"):
        raise HTTPException(status_code=500, detail=f"Ошибка перехода к предыдущему треку: {result['error']}")

    return {"message": "Переход к предыдущему треку выполнен"}


@app.get("/surface/playback-seek/{seconds}")
async def playback_seek(seconds: int):
    """Перемотка вперед/назад в MPV"""
    if not os.path.exists(MPV_SOCKET):
        raise HTTPException(status_code=404, detail="MPV не запущен")

    result = await mpv_command(["seek", str(seconds)])

    if result.get("error"):
        raise HTTPException(status_code=500, detail=f"Ошибка перемотки: {result['error']}")

    return {"message": f"Выполнена перемотка на {seconds} секунд"}


@app.get("/surface/toggle-fill")
async def toggle_fill():
    """Переключение режима заполнения экрана в MPV"""
    if not os.path.exists(MPV_SOCKET):
        raise HTTPException(status_code=404, detail="MPV не запущен")

    # Получаем текущее значение panscan
    panscan_status = await mpv_command(["get_property", "panscan"])

    # Инвертируем текущий режим
    current_panscan = panscan_status.get("data", 0)
    new_panscan = 0 if current_panscan > 0 else 1

    # Устанавливаем новое значение
    result = await mpv_command(["set_property", "panscan", new_panscan])

    if result.get("error"):
        raise HTTPException(status_code=500, detail=f"Ошибка переключения режима заполнения: {result['error']}")

    mode = "с обрезкой (без черных полос)" if new_panscan > 0 else "с черными полосами"
    return {"mode": mode, "panscan": new_panscan, "message": f"Режим заполнения изменен на {mode}"}


@app.get("/surface/kill-all")
async def kill_all():
    """Завершение всех процессов Chrome и MPV"""
    chrome_result = kill_chrome_processes()
    mpv_result = kill_mpv_processes()

    return {
        "message": "Все процессы Chrome и MPV завершены",
        "chrome_success": chrome_result["success"],
        "mpv_success": mpv_result["success"]
    }


@app.get("/surface/reboot")
async def reboot_system():
    """Перезагрузка системы"""
    # Запускаем команду в фоне и не ждем результата
    spawn("sudo /sbin/reboot")

    return {"message": "Запрос на перезагрузку отправлен"}


########################
# AirPlay
########################

AIRPLAY_VIDEO_UNIT = os.environ.get("AIRPLAY_VIDEO_UNIT", "airplay-video.service")
AIRPLAY_AUDIO_UNIT = os.environ.get("AIRPLAY_AUDIO_UNIT", "airplay-audio.service")
AIRPLAY_VIDEO_PORT = int(os.environ.get("AIRPLAY_VIDEO_PORT", "35000"))
AIRPLAY_STATE_FILE = "/var/lib/kiosk/airplay_client"
KIOSK_RESTORE_UNIT = os.environ.get("KIOSK_RESTORE_UNIT", "kiosk-restore.service")


def unit_state(unit: str) -> str:
    result = run_command(f"systemctl is-active {unit}")
    return result["stdout"].strip() or "unknown"


def _airplay_session_clients() -> List[str]:
    """Адреса клиентов с установленными соединениями на зеркальные порты"""
    ports = {AIRPLAY_VIDEO_PORT, AIRPLAY_VIDEO_PORT + 1, AIRPLAY_VIDEO_PORT + 2}
    clients = set()
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if (conn.status == psutil.CONN_ESTABLISHED and conn.laddr
                    and conn.laddr.port in ports and conn.raddr):
                clients.add(conn.raddr.ip)
    except Exception as e:
        logger.error(f"Не удалось перечислить AirPlay-соединения: {e}")
    return sorted(clients)


def _airplay_restore_kiosk():
    """Сбросить флаг сессии и вернуть на экран то, что было до AirPlay"""
    try:
        with open(AIRPLAY_STATE_FILE, "w") as f:
            f.write("0\n")
    except OSError:
        pass
    # Через systemd, а не напрямую: restore, запущенный от пользователя API,
    # погибает от pkill -f chrome в обработчике chrome-tabs — его собственный
    # curl содержит «chrome-tabs» в командной строке. Юнит работает от root,
    # до него этот pkill не дотягивается, а параллельные запуски systemd
    # сериализует сам.
    run_command(f"sudo systemctl restart --no-block {KIOSK_RESTORE_UNIT}")


@app.get("/surface/airplay-state")
async def airplay_state():
    """Состояние приёма AirPlay: сервисы и активная сессия"""
    clients = _airplay_session_clients()
    return {
        "video": unit_state(AIRPLAY_VIDEO_UNIT),
        "audio": unit_state(AIRPLAY_AUDIO_UNIT),
        "session_active": bool(clients),
        "clients": clients,
    }


@app.get("/surface/airplay-on")
async def airplay_on():
    """Включение приёма AirPlay"""
    result = run_command(f"sudo systemctl start {AIRPLAY_VIDEO_UNIT} {AIRPLAY_AUDIO_UNIT}")
    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"Не удалось запустить AirPlay: {result['stderr']}")
    return {
        "message": "AirPlay включён",
        "video": unit_state(AIRPLAY_VIDEO_UNIT),
        "audio": unit_state(AIRPLAY_AUDIO_UNIT),
    }


@app.get("/surface/airplay-off")
async def airplay_off():
    """Выключение приёма AirPlay; экран возвращается к киоску"""
    had_session = bool(_airplay_session_clients())
    result = run_command(f"sudo systemctl stop {AIRPLAY_VIDEO_UNIT} {AIRPLAY_AUDIO_UNIT}")
    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"Не удалось остановить AirPlay: {result['stderr']}")
    # Сторожевой таймер не работает при выключенном сервисе, так что вернуть
    # киоск на экран нужно отсюда — иначе после сессии он останется чёрным.
    _airplay_restore_kiosk()
    return {"message": "AirPlay выключен", "interrupted_session": had_session}


@app.get("/surface/airplay-kick")
async def airplay_kick():
    """Сброс зависшего клиента: перезапуск видеоприёмника AirPlay"""
    clients = _airplay_session_clients()
    result = run_command(f"sudo systemctl restart {AIRPLAY_VIDEO_UNIT}")
    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"Не удалось перезапустить AirPlay: {result['stderr']}")
    _airplay_restore_kiosk()
    return {"message": "AirPlay-приёмник перезапущен", "dropped_clients": clients}


########################
# Диагностика и обслуживание
########################

KIOSK_LOG_FILE = "/var/log/kiosk.log"
SCREENSHOT_FILE = "/tmp/surface_screenshot.png"
HEALTH_UNITS = (
    "xinit.service", "surface-api.service", "net-watchdog.timer",
    "kiosk-restore.service", "kiosk-output.timer",
    "airplay-video.service", "airplay-audio.service", "airplay-watch.timer",
)


@app.get("/surface/screenshot")
async def take_screenshot():
    """Скриншот текущего экрана (PNG)"""
    result = run_command(f"DISPLAY=:0 scrot -o {SCREENSHOT_FILE}")
    if not result["success"] or not os.path.exists(SCREENSHOT_FILE):
        raise HTTPException(status_code=500, detail=f"scrot не отработал: {result['stderr'].strip()}")
    return FileResponse(SCREENSHOT_FILE, media_type="image/png", filename="kiosk-screen.png")


@app.get("/surface/health")
async def health():
    """Сводное состояние киоска одним JSON"""
    units = {unit: unit_state(unit) for unit in HEALTH_UNITS}

    def state_file(name):
        try:
            return open(f"/var/lib/kiosk/{name}").read().strip()
        except OSError:
            return None

    backlight = {}
    for p in pathlib.Path("/sys/class/backlight").glob("*/bl_power"):
        try:
            backlight[p.parent.name] = int(p.read_text().strip())
        except (OSError, ValueError):
            pass

    chromium = sum(1 for p in psutil.process_iter(["name"])
                   if "chrom" in (p.info["name"] or "").lower())
    clients = _airplay_session_clients()

    return {
        "units": units,
        "display": {
            "output": DISPLAY_OUTPUT,
            "active_output": state_file("active_output"),
            # 0 = подсветка горит, 4 = панель погашена
            "backlight": backlight,
        },
        "watchdog_fail_count": state_file("fail_count"),
        "airplay": {"session_active": bool(clients), "clients": clients},
        "browser_processes": chromium,
        "mpv_active": os.path.exists(MPV_SOCKET),
        "power": {
            # 0x0 = питание в порядке; всё другое — троттлинг или недонапряжение
            "throttled": run_command("vcgencmd get_throttled")["stdout"].strip() or None,
            "temperature": run_command("vcgencmd measure_temp")["stdout"].strip() or None,
        },
        "system": {
            "uptime_seconds": int(time.time() - psutil.boot_time()),
            "load_average": os.getloadavg(),
            "memory_used_percent": psutil.virtual_memory().percent,
            "disk_used_percent": psutil.disk_usage("/").percent,
        },
    }


@app.get("/surface/logs")
async def tail_logs(lines: int = Query(50, ge=1, le=1000)):
    """Хвост общего лога киоска (/var/log/kiosk.log)"""
    result = run_command(f"tail -n {lines} {KIOSK_LOG_FILE}")
    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"Не удалось прочитать лог: {result['stderr'].strip()}")
    return {"file": KIOSK_LOG_FILE, "lines": result["stdout"].splitlines()}


OUTPUT_OVERRIDE_FILE = "/var/lib/kiosk/output_override"


def _connected_outputs():
    result = run_command("DISPLAY=:0 xrandr --query | grep ' connected'")
    return [line.split()[0] for line in result["stdout"].splitlines() if line.strip()]


@app.get("/surface/output-state")
async def output_state():
    """Видеовыходы: какие подключены, какой активен, есть ли ручной выбор"""
    def read(path):
        try:
            return open(path).read().strip() or None
        except OSError:
            return None
    return {
        "connected": _connected_outputs(),
        "active": read("/var/lib/kiosk/active_output"),
        "override": read(OUTPUT_OVERRIDE_FILE) or "auto",
    }


@app.get("/surface/output-set/{output}")
async def output_set(output: str):
    """Выбор экрана: имя выхода (HDMI-2, DSI-1, ...) или auto.

    Нужен для выключенного, но воткнутого монитора: он держит линию HPD,
    система считает его подключённым, и автоматика честно выбирает его.
    """
    connected = _connected_outputs()
    if output != "auto" and output not in connected:
        raise HTTPException(status_code=400,
                            detail=f"Выход {output} не подключён. Есть: {', '.join(connected)} и auto")
    try:
        with open(OUTPUT_OVERRIDE_FILE, "w") as f:
            f.write("" if output == "auto" else output + "\n")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить выбор: {e}")
    # Применить сразу, не дожидаясь таймера. Скрипт может перезапустить этот
    # же API (смена выхода) — поэтому в фоне, чтобы успеть ответить.
    spawn("/usr/local/bin/kiosk-output.sh")
    return {"message": f"Экран: {output}", "override": output, "connected": connected}


@app.get("/surface/restore")
async def restore_screen():
    """Вернуть на экран то, что киоск должен показывать (kiosk-restore)"""
    ensure_not_quiet()
    _airplay_restore_kiosk()
    return {"message": "Восстановление запущено, экран вернётся через несколько секунд"}


@app.get("/surface/remote")
async def remote_ui():
    """Веб-пульт: управление киоском с телефона"""
    if not os.path.exists(REMOTE_HTML_FILE):
        raise HTTPException(status_code=404, detail="remote.html не найден рядом с main.py")
    return FileResponse(REMOTE_HTML_FILE, media_type="text/html")


def _remote_asset(filename: str, media_type: str):
    path = os.path.join(CURRENT_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"{filename} не найден")
    return FileResponse(path, media_type=media_type)


@app.get("/surface/remote-manifest.json")
async def remote_manifest():
    """PWA-манифест пульта"""
    return _remote_asset("remote-manifest.json", "application/manifest+json")


@app.get("/surface/remote-icon-192.png")
async def remote_icon_192():
    """Иконка пульта 192x192"""
    return _remote_asset("remote-icon-192.png", "image/png")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Иконка вкладки для всех страниц API (в т.ч. /docs)"""
    return _remote_asset("remote-icon-192.png", "image/png")


@app.get("/surface/remote-icon-512.png")
async def remote_icon_512():
    """Иконка пульта 512x512"""
    return _remote_asset("remote-icon-512.png", "image/png")


@app.post("/surface/custom-command")
async def custom_command(command_req: CustomCommandRequest):
    """Выполнение произвольной команды"""
    # Проверка команды на безопасность можно добавить здесь

    result = run_command(command_req.command)

    return {
        "command": command_req.command,
        "success": result["success"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "exit_code": result["exit_code"]
    }


@app.get("/surface/custom-command-get")
async def custom_command_get(command: str = Query(...)):
    """Выполнение произвольной команды через GET для удобства"""
    result = run_command(command)

    return {
        "command": command,
        "success": result["success"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "exit_code": result["exit_code"]
    }


########################
# TTS — перенесённый с 3B сервис: Google Cloud TTS + OpenAI, кеш, настройки
########################

# Ключи живут в /etc/kiosk/kiosk.env, репозиторий публичный — сюда их не класть.
GOOGLE_TTS_API_KEY = os.environ.get("GOOGLE_TTS_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
OPENAI_TTS_MODEL = "tts-1"
TTS_SETTINGS_FILE = "/var/lib/kiosk/tts.json"
TTS_CACHE_DIR = "/var/lib/kiosk/tts-cache"
TTS_CACHE_MAX_FILES = 400
TTS_VOICES_FILE = os.path.join(CURRENT_DIR, "tts-voices.json")

TTS_DEFAULTS = {
    "volume": 55, "notification_enabled": True, "notification_volume": 60,
    "pause_duration": 0.5,
    "google_voice_ro": "ro-RO-Wavenet-B",
    "google_voice_es": "es-ES-Chirp3-HD-Achird",
    "google_speed": 1, "google_pitch": 0, "google_gain": 0,
    "openai_voice": "alloy", "openai_speed": 1,
}


def load_tts_settings() -> dict:
    merged = dict(TTS_DEFAULTS)
    try:
        with open(TTS_SETTINGS_FILE) as f:
            stored = json.load(f)
        merged.update({k: v for k, v in stored.items() if k in TTS_DEFAULTS})
    except (OSError, ValueError):
        pass
    return merged


def save_tts_settings(settings: dict):
    with open(TTS_SETTINGS_FILE, "w") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def tts_cache_key(provider: str, text: str, voice: str, speed, pitch=0, gain=0) -> str:
    # Схема один в один со старым сервисом на 3B — его кеш скопирован сюда,
    # и старые озвучки продолжают находиться.
    raw = f"{provider}:{voice}:{speed}:{pitch}:{gain}:{text}"
    return hashlib.md5(raw.encode()).hexdigest()


def tts_cache_get(key: str):
    path = os.path.join(TTS_CACHE_DIR, f"{key}.mp3")
    return path if os.path.exists(path) else None


def tts_cache_put(key: str, audio: bytes) -> str:
    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    path = os.path.join(TTS_CACHE_DIR, f"{key}.mp3")
    with open(path, "wb") as f:
        f.write(audio)
    try:
        entries = sorted(pathlib.Path(TTS_CACHE_DIR).glob("*.mp3"), key=os.path.getmtime)
        for stale in entries[:-TTS_CACHE_MAX_FILES]:
            stale.unlink()
    except OSError:
        pass
    return path


def google_tts_synth(text: str, lang: str, voice, settings: dict):
    """Google Cloud TTS → (путь к mp3, из_кеша). Бросает исключение при отказе."""
    if not GOOGLE_TTS_API_KEY:
        raise RuntimeError("GOOGLE_TTS_API_KEY не задан в /etc/kiosk/kiosk.env")
    voice = voice or settings.get(f"google_voice_{lang}")
    if not voice:
        raise RuntimeError(f"для языка {lang} не настроен голос Google")
    speed = settings["google_speed"]
    # Chirp-голоса не принимают pitch и gain
    pitch = 0 if "Chirp" in voice else settings["google_pitch"]
    gain = 0 if "Chirp" in voice else settings["google_gain"]

    key = tts_cache_key("google", text, voice, speed, pitch, gain)
    cached = tts_cache_get(key)
    if cached:
        return cached, True

    audio_config = {"audioEncoding": "MP3", "speakingRate": speed}
    if "Chirp" not in voice:
        audio_config["pitch"] = pitch
        audio_config["volumeGainDb"] = gain
    response = requests.post(
        f"{GOOGLE_TTS_URL}?key={GOOGLE_TTS_API_KEY}",
        json={
            "input": {"text": text},
            "voice": {"languageCode": voice[:5], "name": voice},
            "audioConfig": audio_config,
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Google TTS: HTTP {response.status_code} {response.text[:200]}")
    audio = base64.b64decode(response.json()["audioContent"])
    return tts_cache_put(key, audio), False


def openai_tts_synth(text: str, settings: dict, voice=None):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан в /etc/kiosk/kiosk.env")
    voice = voice or settings["openai_voice"]
    speed = settings["openai_speed"]
    key = tts_cache_key("openai", text, voice, speed)
    cached = tts_cache_get(key)
    if cached:
        return cached, True
    response = requests.post(
        OPENAI_TTS_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={"model": OPENAI_TTS_MODEL, "voice": voice, "input": text, "speed": speed},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"OpenAI TTS: HTTP {response.status_code} {response.text[:200]}")
    return tts_cache_put(key, response.content), False


def gtts_synth(text: str, lang: str, slow: bool):
    from gtts import gTTS
    key = tts_cache_key("gtts", text, f"{lang}{'-slow' if slow else ''}", 1)
    cached = tts_cache_get(key)
    if cached:
        return cached, True
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        gTTS(text=text, lang=lang, slow=slow).save(tmp.name)
        with open(tmp.name, "rb") as f:
            audio = f.read()
    os.unlink(tmp.name)
    return tts_cache_put(key, audio), False


def espeak_synth(text: str, lang: str, slow: bool):
    path = f"/tmp/tts_espeak_{int(time.time())}.wav"
    speed = 110 if slow else 145
    result = run_command(f"espeak-ng -v {lang} -s {speed} -w {path} {shlex.quote(text)}")
    if not result["success"]:
        raise RuntimeError(f"espeak-ng: {result['stderr'].strip()}")
    return path, False


def play_announcement(path: str, volume: int, notification: bool,
                      notification_volume: int, pause: float, keep: bool = True):
    """Сигнал → пауза → речь, всё в фоне; громкость возвращается после."""
    prev = get_current_volume()
    steps = []
    if notification and os.path.exists(NOTIFICATION_FILE):
        steps.append(f"amixer -D pulse sset Master {notification_volume}% >/dev/null")
        steps.append(f"mpg123 -q {NOTIFICATION_FILE}")
        if pause > 0:
            steps.append(f"sleep {pause}")
    steps.append(f"amixer -D pulse sset Master {volume}% >/dev/null")
    player = "mpg123 -q" if path.endswith(".mp3") else "paplay"
    steps.append(f"{player} {shlex.quote(path)}")
    if prev is not None:
        steps.append(f"amixer -D pulse sset Master {prev}% >/dev/null")
    if not keep:
        steps.append(f"rm -f {shlex.quote(path)}")
    spawn("; ".join(steps))
    return prev


@app.get("/surface/tts-say")
async def tts_say(
        text: str = Query(..., min_length=1, max_length=1000, description="Текст для озвучки"),
        lang: str = Query("ro", description="Язык (ro, es, ru, en, ...)"),
        engine: str = Query("auto", description="auto | google | openai | gtts | espeak"),
        voice: str = Query(None, description="Голос (для google/openai), иначе из настроек"),
        volume: int = Query(None, ge=0, le=150, description="Громкость речи, иначе из настроек"),
        notification: bool = Query(None, description="Сигнал перед речью, иначе из настроек"),
        speed: float = Query(None, ge=0.25, le=4.0, description="Скорость речи, иначе из настроек"),
        slow: bool = Query(False, description="Медленная речь (gtts/espeak)"),
):
    """Произнести текст на киоске.

    Цепочка auto: Google Cloud TTS (голоса Wavenet/Chirp3-HD, ключ в
    kiosk.env) → gTTS (обычный голос Google, без ключа) → espeak-ng (офлайн).
    Синтез кешируется — повторные фразы играют мгновенно и бесплатно.
    """
    ensure_not_quiet()
    settings = load_tts_settings()
    volume = settings["volume"] if volume is None else volume
    notification = settings["notification_enabled"] if notification is None else notification
    if speed is not None:
        settings = {**settings, "google_speed": speed, "openai_speed": speed}
        slow = slow or speed < 0.9  # для gtts/espeak, у которых скорость двоичная

    if engine == "auto":
        chain = (["google"] if GOOGLE_TTS_API_KEY else []) + ["gtts", "espeak"]
    else:
        chain = [engine]

    errors = []
    for eng in chain:
        try:
            if eng == "google":
                path, cached = google_tts_synth(text, lang, voice, settings)
                keep = True
            elif eng == "openai":
                path, cached = openai_tts_synth(text, settings, voice)
                keep = True
            elif eng == "gtts":
                path, cached = gtts_synth(text, lang, slow)
                keep = True
            elif eng == "espeak":
                path, cached = espeak_synth(text, lang, slow)
                keep = False
            else:
                raise HTTPException(status_code=400, detail=f"Неизвестный движок: {eng}")
            prev = play_announcement(path, volume, notification,
                                     settings["notification_volume"],
                                     settings["pause_duration"], keep)
            return {"message": "Произношу", "engine": eng, "cached": cached,
                    "text": text, "lang": lang, "volume": volume,
                    "restored_volume": prev}
        except HTTPException:
            raise
        except Exception as e:
            errors.append(f"{eng}: {e}")
            logger.warning(f"TTS {eng} не сработал: {e}")
    raise HTTPException(status_code=502, detail="; ".join(errors))


@app.get("/tts/google")
async def tts_google_compat(
        text: str = Query(..., description="Текст для озвучивания"),
        lang: str = Query("ro", description="Язык"),
        volume: float = Query(None, ge=0.0, le=2.0, description="Громкость 0.0-1.0 (старая шкала)"),
        notification_enabled: bool = Query(None),
        voice: str = Query(None),
        speed: float = Query(None, ge=0.25, le=4.0),
):
    """Совместимость со старым TTS-сервером с 3B (порт 8000): тот же путь и
    параметры — в автоматизациях достаточно поменять адрес на
    http://<киоск>:7000/tts/google"""
    return await tts_say(
        text=text, lang=lang, engine="google", voice=voice,
        volume=None if volume is None else round(volume * 100),
        notification=notification_enabled, speed=speed, slow=False,
    )


@app.get("/tts/openai")
async def tts_openai_compat(
        text: str = Query(..., description="Текст для озвучивания"),
        volume: float = Query(None, ge=0.0, le=2.0),
        notification_enabled: bool = Query(None),
        voice: str = Query(None),
        speed: float = Query(None, ge=0.25, le=4.0),
):
    """Совместимость со старым TTS-сервером с 3B: OpenAI-движок"""
    return await tts_say(
        text=text, lang="ro", engine="openai", voice=voice,
        volume=None if volume is None else round(volume * 100),
        notification=notification_enabled, speed=speed, slow=False,
    )


@app.get("/surface/tts-voices")
async def tts_voices():
    """Доступные голоса (Google по языкам, OpenAI) и текущие настройки"""
    voices = {}
    try:
        with open(TTS_VOICES_FILE) as f:
            voices = json.load(f)
    except (OSError, ValueError):
        pass
    return {"voices": voices, "settings": load_tts_settings(),
            "google_key": bool(GOOGLE_TTS_API_KEY), "openai_key": bool(OPENAI_API_KEY)}


@app.post("/surface/tts-settings")
async def tts_settings_update(update: Dict[str, Any]):
    """Обновить настройки TTS (принимает любые ключи из настроек)"""
    settings = load_tts_settings()
    unknown = [k for k in update if k not in TTS_DEFAULTS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Неизвестные ключи: {', '.join(unknown)}")
    settings.update(update)
    try:
        save_tts_settings(settings)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить: {e}")
    return {"message": "Сохранено", "settings": settings}


@app.post("/surface/tts-play-upload")
async def play_uploaded_audio(
        audio_file: UploadFile = File(..., description="Аудиофайл для воспроизведения"),
        main_volume: int = Form(100, description="Громкость воспроизведения основного аудио (0-150)"),
        play_notification: bool = Form(True, description="Проиграть звук уведомления перед речью"),
        notification_volume: int = Form(80, description="Громкость уведомления (0-150)"),
        delay_after_notification: float = Form(0.5,
                                               description="Задержка в секундах между уведомлением и основным аудио")
):
    """
    Принимает загруженный аудиофайл и воспроизводит его через стандартный аудиовыход
    с опциональным уведомлением перед воспроизведением, отдельными настройками громкости
    и задержкой между звуками
    """
    ensure_not_quiet()
    prev_volume = get_current_volume()
    # Проверяем тип файла (опционально)
    content_type = audio_file.content_type
    if not content_type or not content_type.startswith("audio/"):
        logger.warning(f"Возможно неверный тип файла: {content_type}")

    # Создаем временный файл для сохранения загруженного аудио
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp_file:
        temp_path = temp_file.name
        # Копируем содержимое загруженного файла во временный файл
        content = await audio_file.read()
        temp_file.write(content)

    logger.info(f"Аудиофайл сохранен во временный файл: {temp_path}")

    try:
        # Останавливаем только MPV процессы, не трогая Chrome
        # kill_mpv_processes()

        # Также останавливаем mpg123, если он запущен
        run_command("pkill -f mpg123 || true")

        # Если включен звук уведомления
        notification_duration = 0
        if play_notification and os.path.exists(NOTIFICATION_FILE):
            # Устанавливаем громкость для уведомления
            volume_cmd = f"amixer -D pulse sset Master {notification_volume}%"
            volume_result = run_command(volume_cmd)
            if not volume_result["success"]:
                logger.warning(f"Не удалось установить громкость уведомления: {volume_result['stderr']}")

            # Проигрываем звук уведомления
            notification_cmd = f"mpg123 -q {NOTIFICATION_FILE}"

            # Выполняем команду и ждем завершения
            notification_process = subprocess.Popen(notification_cmd, shell=True)
            notification_process.wait()

            # Получаем длительность звука уведомления
            duration_cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {NOTIFICATION_FILE}"
            duration_result = run_command(duration_cmd)
            if duration_result["success"]:
                try:
                    notification_duration = float(duration_result["stdout"].strip())
                except:
                    notification_duration = 1  # Примерное значение, если не удалось определить

            # Применяем задержку после уведомления, если указана
            if delay_after_notification > 0:
                logger.info(f"Ожидание {delay_after_notification} секунд после уведомления")
                time.sleep(delay_after_notification)

        # Устанавливаем громкость для основного аудио
        volume_cmd = f"amixer -D pulse sset Master {main_volume}%"
        volume_result = run_command(volume_cmd)
        if not volume_result["success"]:
            logger.warning(f"Не удалось установить громкость основного аудио: {volume_result['stderr']}")

        # Определяем тип аудиофайла и выбираем подходящую команду
        play_cmd = ""
        if temp_path.lower().endswith(".mp3"):
            play_cmd = f"mpg123 -q {temp_path}"
        else:
            # Для других форматов используем mpv
            play_cmd = f"mpv --no-video --really-quiet {temp_path}"

        # Запускаем воспроизведение в фоне
        spawn(f"{play_cmd} > /tmp/tts_playback.log 2>&1")

        # Получаем длительность аудио (если возможно)
        duration_cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {temp_path}"
        duration_result = run_command(duration_cmd)

        duration = None
        if duration_result["success"]:
            try:
                duration = float(duration_result["stdout"].strip())
            except (ValueError, TypeError):
                logger.warning(f"Не удалось определить длительность аудио: {duration_result['stdout']}")

        # После воспроизведения вернуть громкость и удалить временный файл
        restore_volume = (f"amixer -D pulse sset Master {prev_volume}% >/dev/null; "
                          if prev_volume is not None else "")
        cleanup_cmd = f"(sleep {(duration or 60) + 1} && {restore_volume}rm -f {temp_path}) &"
        run_command(cleanup_cmd)

        total_time = notification_duration + delay_after_notification + (duration or 0)

        return {
            "message": f"Воспроизведение загруженного аудиофайла",
            "filename": audio_file.filename,
            "main_volume": main_volume,
            "notification": play_notification,
            "notification_played": play_notification and os.path.exists(NOTIFICATION_FILE),
            "notification_volume": notification_volume if play_notification else 0,
            "notification_duration": notification_duration if play_notification else 0,
            "delay_after_notification": delay_after_notification,
            "main_audio_duration": duration,
            "total_duration": total_time,
            "estimated_end_time": time.time() + total_time
        }
    except Exception as e:
        # В случае ошибки удаляем временный файл
        try:
            os.unlink(temp_path)
        except:
            pass
        logger.error(f"Ошибка при воспроизведении загруженного аудио: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка воспроизведения: {str(e)}")


@app.get("/surface/chrome-tabs")
async def chrome_tabs_slideshow(
        urls: List[str] = Query(None, description="URLs для отображения"),
        times: List[int] = Query(None, description="Продолжительность отображения каждого таба в секундах")
):
    """
    Открывает URL в единственном окне Chrome и циклически отображает их через iframe с заданными интервалами.
    """
    ensure_not_quiet()
    # Создаем директорию для логов
    os.makedirs("/tmp/chrome_tabs_logs", exist_ok=True)

    # Сохраняем подробный лог действий
    log_file = f"/tmp/chrome_tabs_logs/debug_{int(time.time())}.log"
    logger.info(f"Подробный лог будет сохранен в: {log_file}")

    # Функция для логирования
    def log_action(message):
        with open(log_file, "a") as f:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{timestamp} - {message}\n")
        logger.info(message)

    log_action("Запуск chrome-tabs")
    log_action(f"Полученные URLs: {urls}")
    log_action(f"Полученные времена: {times}")

    # Останавливаем все текущие процессы воспроизведения
    kill_chrome_processes()
    kill_mpv_processes()
    log_action("Существующие процессы Chrome и MPV завершены")

    if not urls or not times or len(urls) == 0:
        error_msg = "Необходимо указать хотя бы один URL и время отображения"
        log_action(f"Ошибка: {error_msg}")
        raise HTTPException(status_code=400, detail=error_msg)

    # Создаем список URL с временем отображения
    tabs = []
    for i in range(min(len(urls), len(times))):
        current_url = urls[i]

        # Добавляем https:// если не указан протокол
        if not current_url.startswith(("http://", "https://")):
            current_url = "https://" + current_url

        tabs.append({"url": current_url, "time": times[i]})
        log_action(f"Добавлен таб {i + 1}: {current_url}, время: {times[i]} секунд")

    if not tabs:
        error_msg = "Не удалось создать ни одного таба"
        log_action(f"Ошибка: {error_msg}")
        raise HTTPException(status_code=400, detail=error_msg)

    # Создаем HTML страницу с автоматическим переключением iframe
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Chrome Tabs Slideshow</title>
    <meta charset="UTF-8">
    <style>
        body, html {{
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100vh;
            overflow: hidden;
            background: black;
        }}
        #frame-container {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
        }}
        iframe {{
            border: none;
            width: 100%;
            height: 100%;
            position: absolute;
            top: 0;
            left: 0;
        }}
        .hidden {{
            display: none;
        }}
        .debug-panel {{
            position: fixed;
            bottom: 0;
            right: 0;
            background: rgba(0,0,0,0.7);
            color: white;
            padding: 10px;
            font-family: monospace;
            font-size: 12px;
            z-index: 9999;
            display: none;
        }}
    </style>
</head>
<body>
    <div id="frame-container"></div>
    <div class="debug-panel" id="debug">Current: <span id="current-tab">0</span></div>

    <script>
        // Конфигурация табов
        const tabs = {json.dumps([{"url": tab["url"], "time": tab["time"]} for tab in tabs])};
        let currentTab = 0;
        let frameContainer = document.getElementById('frame-container');
        let debugInfo = document.getElementById('debug');
        let currentTabInfo = document.getElementById('current-tab');

        // Функция для создания и загрузки всех iframe
        function setupFrames() {{
            tabs.forEach((tab, index) => {{
                const frame = document.createElement('iframe');
                frame.src = tab.url;
                frame.id = `frame-${{index}}`;
                frame.className = index === 0 ? '' : 'hidden';
                frameContainer.appendChild(frame);

                console.log(`Фрейм ${{index}} создан с URL: ${{tab.url}}`);
            }});

            currentTabInfo.textContent = `1 / ${{tabs.length}} - ${{tabs[0].url}} (${{tabs[0].time}}s)`;
        }}

        // Функция для переключения на следующий таб
        function switchToNextTab() {{
            // Скрываем текущий таб
            document.getElementById(`frame-${{currentTab}}`).className = 'hidden';

            // Переключаемся на следующий таб
            currentTab = (currentTab + 1) % tabs.length;

            // Показываем новый таб
            document.getElementById(`frame-${{currentTab}}`).className = '';

            // Обновляем отладочную информацию
            currentTabInfo.textContent = `${{currentTab + 1}} / ${{tabs.length}} - ${{tabs[currentTab].url}} (${{tabs[currentTab].time}}s)`;

            // Планируем следующее переключение
            setTimeout(switchToNextTab, tabs[currentTab].time * 1000);
        }}

        // Инициализация
        window.onload = function() {{
            console.log('Окно загружено, настройка фреймов...');
            setupFrames();

            // Запускаем цикл переключения
            setTimeout(() => {{
                console.log('Запуск цикла переключения...');
                setTimeout(switchToNextTab, tabs[0].time * 1000);
            }}, 1000);
        }};
    </script>
</body>
</html>
"""

    html_path = f"/tmp/chrome_tabs_{int(time.time())}.html"
    with open(html_path, "w") as f:
        f.write(html_content)
    log_action(f"Создан HTML файл: {html_path}")

    # Запускаем Chrome с нашей HTML страницей
    chrome_cmd = f"""export DISPLAY=:0
xset dpms force on
xset s off
xset s noblank
xset dpms 0 0 0
xrandr --output {DISPLAY_OUTPUT} --auto
/usr/bin/chromium \\
    --kiosk \\
    --app=file://{html_path} \\
    --disable-infobars \\
    --no-first-run \\
    --no-sandbox \\
    --disable-web-security \\
    --disable-popup-blocking \\
    --disable-background-timer-throttling \\
    --autoplay-policy=no-user-gesture-required > /tmp/chrome_tabs.log 2>&1 &"""

    # Команда включает дисплей принудительно — флаг «выключен намеренно» снят.
    set_display_off_flag(False)
    log_action(f"Запуск Chrome с командой: {chrome_cmd}")
    result = run_command(chrome_cmd)
    log_action(f"Результат запуска Chrome: {result}")

    return {
        "success": result["success"],
        "message": f"Запущено слайдшоу с {len(tabs)} страницами",
        "tabs": [{"url": tab['url'], "time": tab['time']} for tab in tabs],
        "html_file": html_path,
        "log_file": log_file
    }


@app.get("/surface/play")
async def play_media(
        url: str = Query(..., description="URL медиа для воспроизведения"),
        volume: int = Query(100, ge=0, le=150, description="Громкость воспроизведения (0-150)"),
        loop: bool = Query(True, description="Повторять воспроизведение"),
        fullscreen: bool = Query(True, description="Полноэкранный режим"),
        quality: int = Query(720, ge=144, le=2160,
                             description="Потолок высоты видео для YouTube. 720 — максимум, который "
                                         "Pi 4 декодит без рывков (софтом; 1080p — ~40 дропов/с)")
):
    """
    Универсальный эндпоинт для воспроизведения медиа через MPV.
    Автоматически определяет тип ссылки и применяет соответствующие параметры.
    Особая обработка для:
    - YouTube видео: автоматическое включение режима радио
    - YouTube плейлисты: правильный запуск с ytdl-raw-options
    - Другие URL: прямой запуск через MPV
    """
    ensure_not_quiet()
    # Завершаем все текущие процессы воспроизведения
    kill_chrome_processes()
    kill_mpv_processes()

    if not url:
        raise HTTPException(status_code=400, detail="URL не указан")

    # Определяем тип URL
    is_youtube_video = ("youtube.com" in url and "v=" in url) or "youtu.be" in url
    is_youtube_playlist = "youtube.com" in url and "list=" in url and not "list=RD" in url
    is_youtube_mix = "youtube.com" in url and "list=RD" in url

    # Базовые параметры MPV
    mpv_params = [
        "--force-window=yes",
        "--input-ipc-server=/tmp/mpvsocket",
        # Именно xv: XVideo масштабирует аппаратно, и 720p играет с
        # единичными дропами. Оба других варианта мерялись и хуже на порядок:
        # --vo=x11 скалирует процессором (~40 дропов/с), --vo=gpu на этой
        # сборке тоже давится (~43 дропа/с на 720p).
        "--vo=xv",
        "--hwdec=auto-safe",
        # Без потолка yt-dlp берёт максимум (VP9/AV1 1080p+), и Pi декодит его
        # софтом с диким тормозом. h264 до 720p Pi умеет аппаратно, а панель
        # всё равно 800x480 — разницы в картинке нет, разница только в FPS.
        f"--ytdl-format=\"bestvideo[height<={quality}][vcodec^=avc1]+bestaudio"
        f"/best[height<={quality}]/best\"",
        f"--volume={volume}",
        "--network-timeout=30",
        "--demuxer-thread=yes",
        "--cache=yes",
        "--cache-secs=30",
        "--force-seekable=yes",
        "--no-terminal",
        "--really-quiet",
        "--input-default-bindings=yes",
        "--input-vo-keyboard=yes",
        "--no-audio-display",
        "--force-window-position"
    ]

    # Добавляем полноэкранный режим, если требуется
    if fullscreen:
        mpv_params.insert(0, "--fullscreen")

    # Добавляем специфичные параметры в зависимости от типа URL
    media_type = "unknown"

    if is_youtube_video and not is_youtube_mix and not is_youtube_playlist:
        # Обычное YouTube видео - добавляем параметры для радио
        media_type = "youtube_video"
        video_id = ""
        if "youtube.com" in url and "v=" in url:
            video_id = url.split("v=")[1].split("&")[0]
        elif "youtu.be" in url:
            video_id = url.split("/")[-1].split("?")[0]

        if video_id:
            url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
            logger.info(f"URL модифицирован для режима радио: {url}")

        mpv_params.append("--ytdl-raw-options=yes-playlist=")
        if loop:
            mpv_params.append("--loop-playlist=inf")

    elif is_youtube_playlist or is_youtube_mix:
        # Для плейлистов и миксов YouTube
        media_type = "youtube_playlist" if is_youtube_playlist else "youtube_mix"
        mpv_params.append("--ytdl-raw-options=yes-playlist=")
        if loop:
            mpv_params.append("--loop-playlist=inf")

    else:
        # Для других типов медиа
        media_type = "generic_url"
        if loop:
            mpv_params.append("--loop-file=inf")

    # Собираем полную команду
    mpv_params_str = " ".join(mpv_params)
    full_cmd = f"export DISPLAY=:0 && xset s off && xset s noblank && xrandr --output {DISPLAY_OUTPUT} --auto && mpv {mpv_params_str} \"{url}\" > /tmp/mpv_play.log 2>&1 &"

    logger.info(f"Запуск команды: {full_cmd}")
    set_display_off_flag(False)
    result = run_command(full_cmd)

    return {
        "message": f"Запуск медиа: {url}",
        "media_type": media_type,
        "success": result["success"],
        "volume": volume,
        "fullscreen": fullscreen,
        "loop": loop,
        "timestamp": time.time()
    }


@app.get("/surface/immich/album")
async def play_immich_album(
        ip: str = Query(..., description="IP-адрес сервера Immich"),
        api_key: str = Query(..., description="API ключ для доступа к Immich"),
        album_id: str = Query(..., description="ID альбома в Immich"),
        limit: int = Query(50, description="Максимальное количество видео в плейлисте"),
        shuffle: bool = Query(True, description="Перемешать видео в плейлисте")
):
    """
    Создает и воспроизводит плейлист видео из указанного альбома Immich
    """
    ensure_not_quiet()
    # Останавливаем все текущие процессы воспроизведения
    kill_chrome_processes()
    kill_mpv_processes()

    try:
        # Получаем данные альбома
        album_url = f"http://{ip}/api/albums/{album_id}"
        headers = {
            "x-api-key": api_key,
            "Accept": "application/json"
        }

        logger.info(f"Запрос данных альбома: {album_url}")
        response = requests.get(album_url, headers=headers, timeout=30)

        if response.status_code != 200:
            logger.error(f"Ошибка получения данных альбома: {response.status_code}")
            raise HTTPException(status_code=response.status_code,
                                detail=f"Ошибка доступа к Immich API: {response.text}")

        album_data = response.json()

        # Проверяем структуру данных
        if not album_data or not album_data.get("assets") or not isinstance(album_data["assets"], list):
            logger.error("Неверная структура данных альбома")
            raise HTTPException(status_code=400, detail="Неверная структура данных альбома")

        # Фильтруем только видео
        videos = [asset for asset in album_data["assets"] if asset.get("type") == "VIDEO"]

        if not videos:
            logger.info("В альбоме не найдено видео")
            raise HTTPException(status_code=404, detail="В альбоме не найдено видео")

        # Перемешиваем видео, если требуется
        if shuffle:
            random.shuffle(videos)

        # Ограничиваем количество видео
        limited_videos = videos[:limit]

        # Создаем плейлист
        logger.info(f"Создание плейлиста из {len(limited_videos)} видео")
        playlist_content = create_immich_playlist(ip, limited_videos)

        # Создаем скрипт для запуска
        script_content = create_immich_script(ip, api_key)

        # Сохраняем плейлист и скрипт
        save_immich_files(playlist_content, script_content)

        # Запускаем скрипт
        result = run_command("/tmp/play_immich.sh")

        if not result["success"]:
            logger.error(f"Ошибка запуска плеера: {result['stderr']}")
            raise HTTPException(status_code=500, detail=f"Ошибка запуска плеера: {result['stderr']}")

        return {
            "status": "success",
            "message": f"Запущено воспроизведение {len(limited_videos)} видео из альбома Immich",
            "videos_count": len(limited_videos),
            "album_id": album_id,
            "album_name": album_data.get("name", "Unknown")
        }

    except requests.RequestException as e:
        logger.error(f"Ошибка сетевого запроса: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ошибка сетевого запроса: {str(e)}")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@app.get("/surface/webcam")
async def show_webcam(
        url: str = Query("http://2.136.193.46:8081/cgi-bin/CGIProxy.fcgi", description="URL веб-камеры"),
        usr: str = Query("Cnb", description="Имя пользователя для веб-камеры"),
        pwd: str = Query("Club@00", description="Пароль для веб-камеры"),
        refresh_rate: int = Query(1000, description="Частота обновления изображения (мс)"),
        location: str = Query("Badalona", description="Название местоположения"),
        show_temp: bool = Query(True, description="Показывать температуру моря"),
        weather_key: str = Query("481ef4eeeb9e4e2ebb395728251203", description="API-ключ worldweatheronline.com"),
        weather_query: str = Query("41.4333,2.2333", description="Координаты для запроса погоды")
):
    """
    Отображает поток с веб-камеры в Chrome в режиме киоска.
    Позволяет отображать информацию о температуре моря.

    Дефолты — публичная камера пляжа Бадалоны: её владельцы сами опубликовали
    доступ у себя на сайте, чтобы люди смотрели. Это не наши учётные данные.
    """
    ensure_not_quiet()
    # Останавливаем все текущие процессы воспроизведения
    kill_chrome_processes()
    kill_mpv_processes()

    show_temp = show_temp and bool(weather_key)

    try:
        # Создаем HTML для отображения веб-камеры
        html_content = create_webcam_html(url, usr, pwd, refresh_rate, location,
                                          show_temp, weather_key, weather_query)

        # Имя файла с временной меткой, чтобы избежать кэширования
        timestamp = int(time.time())
        html_filename = f"/tmp/webcam_{timestamp}.html"

        # Сохраняем HTML в файл
        with open(html_filename, "w") as f:
            f.write(html_content)

        # Делаем файл доступным для чтения
        os.chmod(html_filename, 0o755)

        # Запускаем Chrome в режиме киоска
        chrome_cmd = f"""export DISPLAY=:0 
xset dpms force on
xset s off
xset s noblank
xset dpms 0 0 0
xrandr --output {DISPLAY_OUTPUT} --auto
nohup {CHROME_KIOSK_CMD} --app=file://{html_filename} \\
  --disable-web-security \\
  --autoplay-policy=no-user-gesture-required > /tmp/chrome_webcam.log 2>&1 & disown"""

        # Выполняем команду
        set_display_off_flag(False)
        result = run_command(chrome_cmd)

        if not result["success"]:
            logger.error(f"Ошибка запуска Chrome для веб-камеры: {result['stderr']}")
            raise HTTPException(status_code=500, detail=f"Ошибка запуска Chrome: {result['stderr']}")

        return {
            "status": "webcam_started",
            "url": url,
            "refresh_rate": f"{refresh_rate}ms",
            "location": location,
            "show_temp": show_temp,
            "html_file": html_filename,
            "log_file": "/tmp/chrome_webcam.log"
        }

    except Exception as e:
        logger.error(f"Ошибка при отображении веб-камеры: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


def create_webcam_html(
        url: str,
        usr: str,
        pwd: str,
        refresh_rate: int,
        location: str,
        show_temp: bool,
        weather_key: str = "",
        weather_query: str = ""
) -> str:
    """Создает HTML для отображения веб-камеры с информацией о температуре"""

    # Блоки температуры собираются заранее отдельными f-строками. Раньше они
    # были вложенными обычными литералами внутри общей f-строки: `{{` в них не
    # разэкранировались, и при show_temp=true в страницу попадал синтаксически
    # битый JS, который ломал весь <script> вместе с обновлением картинки.
    weather_div = f"""
        <div id="weather-info">
            <div class="location">{location}</div>
            <div class="temp-label">Temperatura del mar:</div>
            <div class="temp-value" id="sea-temp">
                <span class="loading">Cargando…</span>
            </div>
        </div>
    """ if show_temp else ""

    weather_js = f"""
        // Функция для получения данных о температуре моря
        async function getSeaTemperature() {{
            try {{
                const response = await fetch('https://api.worldweatheronline.com/premium/v1/marine.ashx?key={weather_key}&q={weather_query}&format=json&includeLocation=yes');
                const data = await response.json();

                if (data && data.data && data.data.weather && data.data.weather[0] && data.data.weather[0].hourly) {{
                    // Ближайший трёхчасовой блок прогноза (0, 3, 6, ...)
                    const timeIndex = Math.floor(new Date().getHours() / 3);
                    const hourData = data.data.weather[0].hourly[timeIndex];
                    if (hourData && hourData.waterTemp_C) {{
                        document.getElementById('sea-temp').innerHTML = hourData.waterTemp_C + '°C';
                    }} else {{
                        document.getElementById('sea-temp').innerHTML = 'N/A';
                    }}
                }} else {{
                    document.getElementById('sea-temp').innerHTML = 'N/A';
                }}
            }} catch (error) {{
                console.error('Ошибка при получении температуры:', error);
                document.getElementById('sea-temp').innerHTML = 'N/A';
            }}
        }}
    """ if show_temp else ""

    weather_js_init = """
        // Получение температуры при загрузке страницы и обновление каждые 30 минут
        getSeaTemperature();
        setInterval(getSeaTemperature, 30 * 60 * 1000);
    """ if show_temp else ""

    # Основной шаблон HTML
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Webcam {location}</title>
    <meta charset="UTF-8">
    <style>
        body, html {{
            margin: 0;
            padding: 0;
            height: 100vh;
            overflow: hidden;
            background: black;
            display: flex;
            justify-content: center;
            align-items: center;
            font-family: Arial, sans-serif;
        }}
        #webcam-container {{
            width: 100vw;
            height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            position: relative;
        }}
        #webcam-image {{
            width: 100%;
            height: 100%;
            object-fit: cover;
        }}


        #weather-info {{
            position: absolute;
            top: 0px;
            right: 0px;
            background-color: rgb(0 0 0 / 63%);
            backdrop-filter: blur(10px);
            padding: 24px 35px;
            border-radius: 0px 0px 15px 0px;
            font-size: 42px;
            z-index: 100;
            text-align: center;
        }}

        .location {{
            font-size: 32px;
            margin-bottom: 10px;
            color: #ffcc00;
            font-weight: bold;
        }}
        .temp-label {{
            font-size: 28px;
            margin-bottom: 5px;
            color: #cccccc;
        }}
        .temp-value {{
            font-size: 56px;
            font-weight: bold;
            color: #4fc3f7;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.5);
        }}
        .loading {{
            color: #999;
            font-style: italic;
        }}

        .date-time {{
            position: absolute;
            bottom: 20px;
            right: 20px;
            background-color: rgba(0, 0, 0, 0.6);
            color: white;
            padding: 10px 15px;
            border-radius: 5px;
            font-size: 24px;
        }}
    </style>
</head>
<body>
    <div id="webcam-container">
        <img id="webcam-image" alt="Webcam Feed">

        {weather_div}

        <div class="date-time" id="datetime"></div>
    </div>

    <script>
        // Функция для обновления даты и времени
        function updateDateTime() {{
            const now = new Date();
            const options = {{ 
                weekday: 'long', 
                year: 'numeric', 
                month: 'long', 
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            }};
            document.getElementById('datetime').textContent = now.toLocaleDateString('es-ES', options);
        }}

        // Обновляем время каждую секунду
        updateDateTime();
        setInterval(updateDateTime, 1000);

        {weather_js}

        // Функция обновления изображения веб-камеры
        let counter = 0;
        function refreshWebcam() {{
            const img = document.getElementById('webcam-image');
            counter++;
            img.src = '{url}?cmd=snapPicture2&usr={usr}&pwd={pwd}&' + counter;
        }}

        // Настройка обработчиков событий для изображения
        document.getElementById('webcam-image').onload = function() {{ 
            setTimeout(refreshWebcam, {refresh_rate});
        }};

        document.getElementById('webcam-image').onerror = function() {{
            setTimeout(refreshWebcam, {refresh_rate * 2});
        }};

        // Запуск обновления веб-камеры
        refreshWebcam();

        {weather_js_init}
    </script>
</body>
</html>
"""

    return html


########################
# Часы поверх экрана
########################

# Часы лежат рядом с main.py и называются clock.py. Раньше здесь было зашито
# clock_tk.py — файла с таким именем нет, но запуск «удавался» (команда уходит
# в фон через &, код возврата всегда 0), и часы молча не появлялись.
# Системный python3, не venv: PyQt5 ставится через apt (python3-pyqt5).
CLOCK_CMD = f"/usr/bin/python3 {CURRENT_DIR}/clock.py"
# Скобка в начале — чтобы pgrep/pkill не ловили сам shell, который их
# запускает: его командная строка содержит паттерн (грабли CLAUDE.md §6.13).
CLOCK_PATTERN = f"[/]usr/bin/python3 {CURRENT_DIR}/clock.py"


def clock_pid():
    """PID процесса часов, либо None"""
    result = run_command(f"pgrep -f '{CLOCK_PATTERN}' | head -1")
    pid = result["stdout"].strip()
    return int(pid) if pid.isdigit() else None


def clock_start():
    spawn(f"export DISPLAY=:0 && {CLOCK_CMD} > /tmp/overlay_clock.log 2>&1")
    time.sleep(0.5)
    return clock_pid() is not None


def clock_stop():
    run_command(f"pkill -9 -f '{CLOCK_PATTERN}' || true")
    return clock_pid() is None


@app.get("/surface/clock-show")
async def show_clock():
    """Запускает отображение часов на экране"""
    ensure_not_quiet()
    if clock_start():
        return {"status": "success", "message": "Часы запущены", "log_file": "/tmp/overlay_clock.log"}
    raise HTTPException(status_code=500, detail="Часы не запустились, смотри /tmp/overlay_clock.log")


@app.get("/surface/clock-hide")
async def hide_clock():
    """Останавливает отображение часов"""
    stopped = clock_stop()
    return {
        "status": "success" if stopped else "failed",
        "message": "Часы остановлены" if stopped else "Не удалось остановить часы"
    }


@app.get("/surface/clock-status")
async def clock_status():
    """Проверяет, запущены ли часы"""
    pid = clock_pid()
    return {"status": "running" if pid else "stopped", "pid": pid}


@app.get("/surface/clock-toggle")
async def toggle_clock():
    """Переключает отображение часов (вкл/выкл)"""
    if clock_pid():
        stopped = clock_stop()
        return {
            "status": "success" if stopped else "failed",
            "message": "Часы остановлены" if stopped else "Не удалось остановить часы",
            "action": "stop"
        }
    ensure_not_quiet()
    started = clock_start()
    return {
        "status": "success" if started else "failed",
        "message": "Часы запущены" if started else "Не удалось запустить часы",
        "action": "start",
        "log_file": "/tmp/overlay_clock.log" if started else None
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7000, reload=False)