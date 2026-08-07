#!/usr/bin/env python3
"""Web server for USB remote control configuration and action execution."""

import json
import os
import subprocess
import threading
import time
import urllib.request

from flask import Flask, jsonify, render_template, request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

app = Flask(__name__)

# Lazy-loaded whisper model (kept in memory after first use)
_whisper_model = None
_whisper_lock = threading.Lock()

# In-memory voice processing log (last 50 entries)
_voice_log = []
_voice_log_lock = threading.Lock()
VOICE_LOG_MAX = 50


def voice_log_append(entry):
    """Append an entry to the voice log (thread-safe, capped)."""
    entry["ts"] = time.time()
    with _voice_log_lock:
        _voice_log.append(entry)
        if len(_voice_log) > VOICE_LOG_MAX:
            del _voice_log[: len(_voice_log) - VOICE_LOG_MAX]


def get_whisper_model():
    """Load whisper model lazily on first voice request."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        config = load_config()
        voice_cfg = config.get("_voice", {})
        model_size = voice_cfg.get("whisper_model", "small")
        print(f"Loading whisper model: {model_size}")
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
        print(f"Whisper model loaded: {model_size}")
        return _whisper_model


def reload_whisper_model():
    """Force reload whisper model (after config change)."""
    global _whisper_model
    with _whisper_lock:
        _whisper_model = None


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(data):
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def execute_actions(actions):
    """Execute a list of actions sequentially in a background thread."""
    for action in actions:
        atype = action.get("type")
        try:
            if atype == "get":
                url = action.get("url", "")
                if url:
                    print(f"  GET {url}")
                    urllib.request.urlopen(url, timeout=10)
            elif atype == "bash":
                cmd = action.get("command", "")
                if cmd:
                    print(f"  BASH: {cmd}")
                    subprocess.run(cmd, shell=True, timeout=30)
            elif atype == "pause":
                seconds = float(action.get("seconds", 1))
                print(f"  PAUSE {seconds}s")
                time.sleep(seconds)
        except Exception as e:
            print(f"  Error executing {atype}: {e}")


def parse_llm_json(text):
    """Extract JSON from LLM response text, even if wrapped in markdown or explanation."""
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to extract JSON from markdown code block
    import re
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try to find first {...} in text
    m = re.search(r'\{[^{}]*\}', text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def build_commands_prompt(commands):
    """Build the system prompt for voice command matching."""
    commands_text = "\n".join(
        f'{i+1}. "{cmd["name"]}" (keywords: {cmd["description"]})'
        for i, cmd in enumerate(commands)
    )
    return (
        "You are a voice command interpreter for a smart home remote control.\n"
        "Match the user's voice command to the most appropriate available command.\n\n"
        "MATCHING RULES:\n"
        "- Match by INTENT and MEANING, not exact words\n"
        "- The user may speak in any language (Romanian, Russian, English, etc.)\n"
        "- Each command has keywords/synonyms — match if the user's words are similar in meaning\n"
        "- Be very flexible. Partial matches and related concepts count.\n\n"
        f"Available commands:\n{commands_text}\n\n"
        'Respond with ONLY a JSON object, no other text:\n'
        '{"action": "execute", "index": 0}\n'
        'or {"action": "unknown"}\n'
        "Index is 0-based."
    )


def transcribe_openai_api(voice_cfg, audio_path):
    """Transcribe audio via OpenAI Whisper API (cloud, fast ~1s)."""
    import requests as req

    api_key = voice_cfg.get("openai_api_key", "")
    if not api_key:
        return None

    # Build prompt from command names + keywords to guide Whisper
    commands = voice_cfg.get("commands", [])
    hint_parts = []
    for cmd in commands:
        hint_parts.append(cmd.get("name", ""))
        hint_parts.append(cmd.get("description", ""))
    prompt_hint = ", ".join(p for p in hint_parts if p)

    try:
        with open(audio_path, "rb") as f:
            form_data = {"model": "whisper-1"}
            language = voice_cfg.get("language", "")
            if language:
                form_data["language"] = language
            if prompt_hint:
                form_data["prompt"] = prompt_hint
            resp = req.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("voice.wav", f, "audio/wav")},
                data=form_data,
                timeout=15,
            )
        if resp.status_code != 200:
            print(f"  OpenAI Whisper API error {resp.status_code}: {resp.text[:300]}")
            return None
        text = resp.json().get("text", "").strip()
        print(f"  OpenAI Whisper API: {text}")
        return text
    except Exception as e:
        print(f"  OpenAI Whisper API error: {e}")
        return None


def call_llm_text(voice_cfg, transcribed_text):
    """Call LLM with text (fallback when audio mode fails)."""
    import requests as req

    commands = voice_cfg.get("commands", [])
    if not commands:
        return {"action": "unknown", "reason": "no commands configured"}

    system_prompt = build_commands_prompt(commands)
    user_message = f'User said: "{transcribed_text}"'
    provider = voice_cfg.get("provider", "claude")

    try:
        if provider == "claude":
            api_key = voice_cfg.get("claude_api_key", "")
            model = voice_cfg.get("claude_model", "claude-haiku-4-5-20251001")
            if not api_key:
                return {"action": "unknown", "reason": "claude API key not set"}
            resp = req.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 100,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                },
                timeout=15,
            )
            if resp.status_code != 200:
                err = resp.text[:300]
                return {"action": "unknown", "reason": f"API {resp.status_code}: {err}"}
            data = resp.json()
            raw_text = data["content"][0]["text"]
            print(f"  LLM raw: {raw_text}")
            parsed = parse_llm_json(raw_text)
            if parsed:
                return parsed
            return {"action": "unknown", "reason": f"bad JSON: {raw_text[:200]}"}

        elif provider == "openai":
            api_key = voice_cfg.get("openai_api_key", "")
            model = voice_cfg.get("openai_model", "gpt-4o-mini")
            if not api_key:
                return {"action": "unknown", "reason": "openai API key not set"}
            resp = req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 100,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                },
                timeout=15,
            )
            if resp.status_code != 200:
                err = resp.text[:300]
                return {"action": "unknown", "reason": f"API {resp.status_code}: {err}"}
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"]
            print(f"  LLM raw: {raw_text}")
            parsed = parse_llm_json(raw_text)
            if parsed:
                return parsed
            return {"action": "unknown", "reason": f"bad JSON: {raw_text[:200]}"}

        else:
            return {"action": "unknown", "reason": f"unknown provider: {provider}"}

    except Exception as e:
        print(f"  LLM error: {e}")
        return {"action": "unknown", "reason": str(e)}


def process_voice_command(audio_path, audio_duration=0):
    """Process voice command: transcribe (cloud or local) then LLM match."""
    t0 = time.time()
    dur_str = f"{audio_duration:.1f}s" if audio_duration else ""
    log_entry = {"steps": [{"step": "record", "detail": f"hold {dur_str}"}]}
    config = load_config()
    voice_cfg = config.get("_voice", {})

    if not voice_cfg.get("enabled", False):
        log_entry["steps"].append({"step": "error", "detail": "voice disabled"})
        voice_log_append(log_entry)
        return {"ok": False, "error": "voice disabled"}

    provider = voice_cfg.get("provider", "claude")
    commands = voice_cfg.get("commands", [])
    text = None

    # Step 1: Transcribe via OpenAI Whisper API
    t1 = time.time()
    log_entry["steps"].append({"step": "transcribe", "detail": "OpenAI Whisper API..."})
    text = transcribe_openai_api(voice_cfg, audio_path)
    t_transcribe = time.time() - t1
    if text:
        print(f"  Transcribe ({t_transcribe:.1f}s): {text}")
        log_entry["steps"].append({"step": "speech", "detail": f"{text} ({t_transcribe:.1f}s)"})

    if not text:
        log_entry["steps"].append({"step": "error", "detail": "empty transcription"})
        voice_log_append(log_entry)
        return {"ok": False, "error": "empty transcription"}

    # Step 2: LLM command matching
    llm_model = voice_cfg.get(f"{provider}_model", "")
    t3 = time.time()
    log_entry["steps"].append({"step": "llm_call", "detail": f"{provider} / {llm_model}"})
    llm_result = call_llm_text(voice_cfg, text)
    t_llm = time.time() - t3
    print(f"  LLM ({t_llm:.1f}s): {llm_result}")
    log_entry["steps"].append({"step": "llm_result", "detail": f"{json.dumps(llm_result)} ({t_llm:.1f}s)"})

    # Step 3: Execute matched command
    action = llm_result.get("action")

    if action == "execute":
        index = llm_result.get("index", -1)
        if 0 <= index < len(commands):
            cmd = commands[index]
            url = cmd.get("url", "")
            log_entry["command"] = cmd["name"]
            log_entry["url"] = url
            t_exec_start = time.time()
            print(f"  Executing: {cmd['name']} -> GET {url}")
            try:
                urllib.request.urlopen(url, timeout=10)
                t_exec = time.time() - t_exec_start
                t_total = time.time() - t0
                log_entry["steps"].append({"step": "execute", "detail": f"{cmd['name']} -> {url} ({t_exec:.1f}s)"})
                log_entry["steps"].append({"step": "done", "detail": f"total {t_total:.1f}s"})
            except Exception as e:
                print(f"  Error executing: {e}")
                log_entry["steps"].append({"step": "execute", "detail": f"{cmd['name']} -> {url}"})
                log_entry["steps"].append({"step": "error", "detail": str(e)})
            voice_log_append(log_entry)
            return {"ok": True, "text": text, "command": cmd["name"], "url": url}
        else:
            log_entry["steps"].append({"step": "error", "detail": f"invalid index: {index}"})
            voice_log_append(log_entry)
            return {"ok": False, "error": f"invalid index: {index}"}
    else:
        t_total = time.time() - t0
        log_entry["steps"].append({"step": "unknown", "detail": f"no match (total {t_total:.1f}s)"})
        voice_log_append(log_entry)
        return {"ok": True, "text": text, "action": "unknown"}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def post_config():
    data = request.get_json()
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400
    save_config(data)
    return jsonify({"ok": True})


@app.route("/api/execute", methods=["POST"])
def execute():
    data = request.get_json()
    button = data.get("button")
    press_type = data.get("type", "press")

    config = load_config()
    btn_config = config.get(button, {})

    if press_type == "press":
        actions = btn_config.get("press", [])
    elif press_type == "longpress":
        lp = btn_config.get("longpress", {})
        actions = lp.get("actions", [])
    else:
        return jsonify({"error": "Unknown type"}), 400

    # Log voice button short press to voice log
    if button == "voice":
        voice_log_append({
            "steps": [
                {"step": "button", "detail": f"short press ({press_type})"},
                {"step": "execute", "detail": f"{len(actions)} action(s)"},
            ],
        })

    if not actions:
        print(f"No actions for {button} ({press_type})")
        return jsonify({"ok": True, "actions": 0})

    print(f"Executing {len(actions)} action(s) for {button} ({press_type})")
    thread = threading.Thread(target=execute_actions, args=(actions,), daemon=True)
    thread.start()

    return jsonify({"ok": True, "actions": len(actions)})


@app.route("/api/voice", methods=["POST"])
def voice():
    data = request.get_json()
    audio_path = data.get("audio_path", "")
    if not audio_path or not os.path.exists(audio_path):
        voice_log_append({
            "steps": [
                {"step": "button", "detail": "long press (voice)"},
                {"step": "error", "detail": f"audio file not found: {audio_path}"},
            ],
        })
        return jsonify({"ok": False, "error": "audio file not found"}), 400

    # Calculate audio duration from wav file (16000 Hz, 16-bit mono, 44-byte header)
    audio_duration = 0
    try:
        file_size = os.path.getsize(audio_path)
        audio_duration = max(0, (file_size - 44)) / (16000 * 2)
        print(f"Processing voice command: {audio_path} ({audio_duration:.1f}s, {file_size} bytes)")
    except Exception:
        print(f"Processing voice command: {audio_path}")

    result = process_voice_command(audio_path, audio_duration)
    return jsonify(result)


@app.route("/api/voice-log", methods=["GET"])
def get_voice_log():
    with _voice_log_lock:
        return jsonify(list(_voice_log))


@app.route("/api/voice-config", methods=["GET"])
def get_voice_config():
    config = load_config()
    voice_cfg = config.get("_voice", {
        "enabled": True,
        "provider": "claude",
        "claude_api_key": "",
        "claude_model": "claude-haiku-4-5-20251001",
        "openai_api_key": "",
        "openai_model": "gpt-4o-mini",
        "whisper_model": "small",
        "language": "en",
        "audio_device": "plughw:0,0",
        "commands": [],
    })
    return jsonify(voice_cfg)


@app.route("/api/voice-config", methods=["POST"])
def post_voice_config():
    data = request.get_json()
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400

    config = load_config()
    old_voice = config.get("_voice", {})
    config["_voice"] = data
    save_config(config)

    # Reload whisper model if model size changed
    if data.get("whisper_model") != old_voice.get("whisper_model"):
        reload_whisper_model()

    return jsonify({"ok": True})


if __name__ == "__main__":
    # На киоске порт 5000 занят shairport-sync (AirPlay-аудио), поэтому порт
    # берётся из окружения; дефолт оставлен родной.
    app.run(host="0.0.0.0", port=int(os.environ.get("REMOTE_PORT", "5000")))
