from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import json
import os
import re
import time
import asyncio
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, AnyHttpUrl
import socket
import psutil
import logging
from contextlib import contextmanager
from enum import Enum
import pathlib
import uvicorn
import random

import requests

import json
from fastapi import Query

import tempfile
from fastapi import UploadFile, File, Form

import pathlib

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
CHROME_KIOSK_CMD = "/usr/bin/chromium --kiosk --disable-infobars --no-first-run --no-sandbox --window-size=1920,1080 --window-position=0,0"
DEFAULT_HTML_DIR = "/tmp/surface_control"

# Создаем директории, если не существуют
os.makedirs(DEFAULT_HTML_DIR, exist_ok=True)


########################
# Вспомогательные классы и функции
########################

class AudioOutput(BaseModel):
    id: str
    name: str
    description: str
    active: bool


class VideoSource(Enum):
    YOUTUBE = "youtube"
    STREAM = "stream"
    YOUTUBE_PLAYLIST = "youtube_playlist"
    LOCAL = "local"


class SlideshowItem(BaseModel):
    url: str
    time: int


class CustomCommandRequest(BaseModel):
    command: str


class ChromeSlideshowRequest(BaseModel):
    slides: List[SlideshowItem]
    transition_duration: int = 1


# Контекстный менеджер для выполнения команд в фоне
@contextmanager
def background_process():
    process = None
    try:
        yield lambda cmd: subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        if process and process.poll() is None:
            process.terminate()


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


# Функция для запуска Chrome с теми же параметрами, как в вашем n8n скрипте
def run_chrome_slideshow(html_path: str) -> dict:
    """Запускает Chrome с параметрами из n8n скрипта"""

    # Параметры запуска Chrome точно как в вашем n8n скрипте
    chrome_cmd = f"""export DISPLAY=:0
xset dpms force on
xset s off
xset s noblank
xset dpms 0 0 0
xrandr --output HDMI-1 --auto
/usr/bin/chromium \\
    --kiosk \\
    --app=file://{html_path} \\
    --disable-infobars \\
    --no-first-run \\
    --window-size=1920,1080 \\
    --window-position=0,0 \\
    --disable-web-security \\
    --disable-session-crashed-bubble \\
    --disable-popup-blocking \\
    --disable-background-timer-throttling \\
    --disable-backgrounding-occluded-windows \\
    --disable-renderer-backgrounding > /tmp/chrome_slideshow.log 2>&1 &"""

    # Выполняем команду запуска
    result = run_command(chrome_cmd)

    return {
        "success": result["success"],
        "command": chrome_cmd,
        "html_path": html_path
    }


# Функция для создания скрипта воспроизведения YouTube

def create_immich_playlist(ip: str, api_key: str, videos: List[dict]) -> str:
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
xrandr --output HDMI-1 --auto

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
    return {
        "name": "Surface Control API",
        "version": "1.0.0",
        "description": "API для управления отображением и аудио на Surface мини-компьютере",
        "endpoints": {
            "GET /surface/status": "Получение статуса системы",
            "GET /surface/processes": "Список запущенных процессов",
            "GET /surface/display-state": "Статус дисплея (on/off)",
            "GET /surface/display-on": "Включение дисплея",
            "GET /surface/display-off": "Выключение дисплея",
            "GET /surface/playback-status": "Статус воспроизведения MPV",
            "GET /surface/audio-outputs": "Список аудио выходов",
            "GET /surface/system-volume": "Уровень громкости",
            "POST /surface/system-volume": "Установка громкости",
            # И другие эндпоинты...
        }
    }


@app.get("/surface/status")
async def system_status():
    """Получение общего статуса системы"""
    try:
        # Состояние дисплея
        display_state_cmd = "DISPLAY=:0 xrandr --query | grep '^HDMI-1'"
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
        volume_cmd = "amixer -D pulse get Master | grep -o '[0-9]*%' | head -1 | tr -d '%'"
        volume_result = run_command(volume_cmd)
        volume_stdout = volume_result["stdout"].strip()
        volume = int(volume_stdout) if volume_result["success"] and volume_stdout else 0

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
    display_cmd = "DISPLAY=:0 xrandr --query | grep '^HDMI-1'"
    result = run_command(display_cmd)

    if result["success"]:
        # Активный выход содержит геометрию вида 1920x1080+0+0; если её нет — экран выключен
        state = "on" if re.search(r"\d+x\d+\+\d+\+\d+", result["stdout"]) else "off"
        return {"state": state}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка получения состояния дисплея: {result['stderr']}")


@app.get("/surface/display-on")
async def turn_display_on():
    """Включение дисплея"""
    cmd = "export DISPLAY=:0 && xset s off && xset s noblank && xrandr --output HDMI-1 --auto"
    result = run_command(cmd)

    if result["success"]:
        return {"state": "on", "message": "Дисплей включен"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка включения дисплея: {result['stderr']}")


@app.get("/surface/display-off")
async def turn_display_off():
    """Выключение дисплея"""
    cmd = "export DISPLAY=:0 && xrandr --output HDMI-1 --off"
    result = run_command(cmd)

    if result["success"]:
        return {"state": "off", "message": "Дисплей выключен"}
    else:
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
    cmd = "amixer -D pulse get Master | grep -o '[0-9]*%' | head -1 | tr -d '%'"
    result = run_command(cmd)
    if result["success"]:
        volume_str = result["stdout"].strip()
        if not volume_str:
            # Обработка случая с пустой строкой
            raise HTTPException(status_code=500, detail="Получена пустая строка при запросе громкости")
        try:
            volume = int(volume_str)
            return {"volume": volume}
        except ValueError:
            raise HTTPException(status_code=500, detail=f"Не удалось преобразовать громкость '{volume_str}' в число")
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка получения громкости: {result['stderr']}")


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
        # Получаем текущее значение громкости
        volume_cmd = "amixer -D pulse get Master | grep -o '[0-9]*%' | head -1 | tr -d '%'"
        volume_result = run_command(volume_cmd)

        if volume_result["success"]:
            try:
                volume = int(volume_result["stdout"].strip())
                return {"volume": volume, "message": f"Громкость увеличена до {volume}%"}
            except ValueError:
                return {"success": True, "message": "Громкость увеличена на 5%"}
        else:
            return {"success": True, "message": "Громкость увеличена на 5%"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка увеличения громкости: {result['stderr']}")


@app.get("/surface/system-volume-down")
async def system_volume_down():
    """Уменьшение громкости системы на 5%"""
    cmd = "amixer -D pulse sset Master 5%-"
    result = run_command(cmd)

    if result["success"]:
        # Получаем текущее значение громкости
        volume_cmd = "amixer -D pulse get Master | grep -o '[0-9]*%' | head -1 | tr -d '%'"
        volume_result = run_command(volume_cmd)

        if volume_result["success"]:
            try:
                volume = int(volume_result["stdout"].strip())
                return {"volume": volume, "message": f"Громкость уменьшена до {volume}%"}
            except ValueError:
                return {"success": True, "message": "Громкость уменьшена на 5%"}
        else:
            return {"success": True, "message": "Громкость уменьшена на 5%"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка уменьшения громкости: {result['stderr']}")


@app.get("/surface/set-audio-output/{sink_id}")
async def set_audio_output(sink_id: str):
    """Переключение аудио выхода"""
    cmd = f"pactl set-default-sink {sink_id}"
    result = run_command(cmd)

    if result["success"]:
        return {"sink_id": sink_id, "message": f"Аудио выход изменен на {sink_id}"}
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка изменения аудио выхода: {result['stderr']}")


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
    cmd = "sudo /sbin/reboot"

    # Запускаем команду в фоне и не ждем результата
    with background_process() as run_bg:
        run_bg(cmd)

    return {"message": "Запрос на перезагрузку отправлен"}


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


# Определяем путь к файлу уведомления относительно текущего py файла
CURRENT_DIR = pathlib.Path(__file__).parent.absolute()
NOTIFICATION_FILE = os.path.join(CURRENT_DIR, "notification.mp3")


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
        with background_process() as run_bg:
            process = run_bg(f"{play_cmd} > /tmp/tts_playback.log 2>&1")

        # Получаем длительность аудио (если возможно)
        duration_cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {temp_path}"
        duration_result = run_command(duration_cmd)

        duration = None
        if duration_result["success"]:
            try:
                duration = float(duration_result["stdout"].strip())
            except (ValueError, TypeError):
                logger.warning(f"Не удалось определить длительность аудио: {duration_result['stdout']}")

        # Настраиваем автоматическое удаление временного файла после воспроизведения
        cleanup_cmd = f"(sleep {(duration or 60) + 1} && rm -f {temp_path}) &"
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
    # Создаем директорию для логов
    run_command("mkdir -p /tmp/chrome_tabs_logs")

    kill_chrome_processes()
    kill_mpv_processes()

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
xrandr --output HDMI-1 --auto
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
        fullscreen: bool = Query(True, description="Полноэкранный режим")
):
    """
    Универсальный эндпоинт для воспроизведения медиа через MPV.
    Автоматически определяет тип ссылки и применяет соответствующие параметры.
    Особая обработка для:
    - YouTube видео: автоматическое включение режима радио
    - YouTube плейлисты: правильный запуск с ytdl-raw-options
    - Другие URL: прямой запуск через MPV
    """
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
        "--vo=x11",
        "--hwdec=auto-safe",
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
    full_cmd = f"export DISPLAY=:0 && xset s off && xset s noblank && xrandr --output HDMI-1 --auto && mpv {mpv_params_str} \"{url}\" > /tmp/mpv_play.log 2>&1 &"

    logger.info(f"Запуск команды: {full_cmd}")
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
        playlist_content = create_immich_playlist(ip, api_key, limited_videos)

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
        show_temp: bool = Query(True, description="Показывать информацию о температуре")
):
    """
    Отображает поток с веб-камеры в Chrome в режиме киоска.
    Позволяет отображать информацию о температуре моря.
    """
    # Останавливаем все текущие процессы воспроизведения
    kill_chrome_processes()
    kill_mpv_processes()

    try:
        # Создаем HTML для отображения веб-камеры
        html_content = create_webcam_html(url, usr, pwd, refresh_rate, location, show_temp)

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
xrandr --output HDMI-1 --auto
nohup {CHROME_KIOSK_CMD} --app=file://{html_filename} \\
  --disable-web-security \\
  --autoplay-policy=no-user-gesture-required > /tmp/chrome_webcam.log 2>&1 & disown"""

        # Выполняем команду
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
        show_temp: bool
) -> str:
    """Создает HTML для отображения веб-камеры с информацией о температуре"""

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

        {'''
        <div id="weather-info">
            <div class="location">{}</div>
            <div class="temp-label">Sea temperature:</div>
            <div class="temp-value" id="sea-temp">
                <span class="loading">Loading...</span>
            </div>
        </div>
        '''.format(location) if show_temp else ''}

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
            document.getElementById('datetime').textContent = now.toLocaleDateString('ru-RU', options);
        }}

        // Обновляем время каждую секунду
        updateDateTime();
        setInterval(updateDateTime, 1000);

        {'''
        // Функция для получения данных о температуре моря
        async function getSeaTemperature() {{
            try {{
                const response = await fetch('https://api.worldweatheronline.com/premium/v1/marine.ashx?key=481ef4eeeb9e4e2ebb395728251203&q=41.4333,2.2333&format=json&includeLocation=yes');
                const data = await response.json();

                if (data && data.data && data.data.weather && data.data.weather[0] && data.data.weather[0].hourly) {{
                    // Получаем текущий час
                    const currentHour = new Date().getHours();
                    // Определяем ближайший временной блок (0, 3, 6, 9, 12, 15, 18, 21)
                    const timeIndex = Math.floor(currentHour / 3);
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
        ''' if show_temp else ''}

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

        {'''
        // Получение температуры при загрузке страницы
        getSeaTemperature();

        // Обновление температуры каждые 30 минут
        setInterval(getSeaTemperature, 30 * 60 * 1000);
        ''' if show_temp else ''}
    </script>
</body>
</html>
"""

    return html


# Эндпоинты для добавления в main.py
@app.get("/surface/clock-show")
async def show_clock():
    """Запускает отображение часов на экране"""
    # Remove these two lines:
    # kill_chrome_processes()
    # kill_mpv_processes()

    # Запускаем часы из файла clock_tk.py
    cmd = "export DISPLAY=:0 && /usr/bin/python3 clock_tk.py > /tmp/overlay_clock.log 2>&1 &"
    result = run_command(cmd)

    if result["success"]:
        return {
            "status": "success",
            "message": "Часы запущены",
            "log_file": "/tmp/overlay_clock.log"
        }
    else:
        raise HTTPException(status_code=500, detail=f"Ошибка запуска часов: {result['stderr']}")


@app.get("/surface/clock-hide")
async def hide_clock():
    """Останавливает отображение часов"""
    # Более надежный способ завершения процесса
    cmd = "pkill -9 -f '/usr/bin/python3 clock_tk.py' || true"
    result = run_command(cmd)

    # Проверяем, что процесс действительно остановлен
    check_cmd = "ps aux | grep '/usr/bin/python3 clock_tk.py' | grep -v grep || echo 'not_running'"
    check_result = run_command(check_cmd)
    is_stopped = "not_running" in check_result["stdout"] or check_result["stdout"].strip() == ""

    return {
        "status": "success" if is_stopped else "failed",
        "message": "Часы остановлены" if is_stopped else "Не удалось остановить часы"
    }


@app.get("/surface/clock-status")
async def clock_status():
    """Проверяет, запущены ли часы"""
    # Более точная проверка процесса
    cmd = "ps aux | grep '/usr/bin/python3 clock_tk.py' | grep -v grep || echo 'not_running'"
    result = run_command(cmd)

    is_running = "not_running" not in result["stdout"] and result["stdout"].strip() != ""

    pid = None
    if is_running and len(result["stdout"].split()) > 1:
        try:
            pid = int(result["stdout"].split()[1])
        except (ValueError, IndexError):
            pass

    return {
        "status": "running" if is_running else "stopped",
        "pid": pid
    }


@app.get("/surface/clock-toggle")
async def toggle_clock():
    """Переключает отображение часов (вкл/выкл)"""
    # Используем более надежный способ проверки запущенных часов
    cmd = "ps aux | grep '/usr/bin/python3 clock_tk.py' | grep -v grep || echo 'not_running'"
    result = run_command(cmd)

    is_running = "not_running" not in result["stdout"] and result["stdout"].strip() != ""

    if is_running:
        # Часы запущены, останавливаем их
        kill_cmd = "pkill -9 -f '/usr/bin/python3 clock_tk.py' || true"
        kill_result = run_command(kill_cmd)

        # Проверяем, что процесс действительно остановлен
        check_cmd = "ps aux | grep '/usr/bin/python3 clock_tk.py' | grep -v grep || echo 'not_running'"
        check_result = run_command(check_cmd)
        is_stopped = "not_running" in check_result["stdout"] or check_result["stdout"].strip() == ""

        return {
            "status": "success" if is_stopped else "failed",
            "message": "Часы остановлены" if is_stopped else "Не удалось остановить часы",
            "action": "stop"
        }
    else:
        # Часы не запущены, запускаем их
        start_cmd = "export DISPLAY=:0 && /usr/bin/python3 clock_tk.py > /tmp/overlay_clock.log 2>&1 &"
        start_result = run_command(start_cmd)

        # Даем процессу время на запуск
        time.sleep(0.5)

        # Проверяем, что процесс запущен
        check_cmd = "ps aux | grep '/usr/bin/python3 clock_tk.py' | grep -v grep || echo 'not_running'"
        check_result = run_command(check_cmd)
        is_started = "not_running" not in check_result["stdout"] and check_result["stdout"].strip() != ""

        return {
            "status": "success" if is_started else "failed",
            "message": "Часы запущены" if is_started else "Не удалось запустить часы",
            "action": "start",
            "log_file": "/tmp/overlay_clock.log" if is_started else None
        }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7000, reload=False)