#!/usr/bin/env python3
"""
Narrator sub-agent: monitors a terminal session and narrates via TTS.

Usage: python3 narrator.py <session_id>

Runs as a background process. Polls the session output, extracts new
meaningful text, uses Claude CLI to generate brief narration,
sends narration to TTS, and pushes audio URL to the main app.
"""

import json
import os
import re
import subprocess
import sys
import time

import httpx

SESSION_ID = sys.argv[1] if len(sys.argv) > 1 else ""
if not SESSION_ID:
    print("Usage: narrator.py <session_id>")
    sys.exit(1)

BASE_URL = f"http://localhost:{os.getenv('LLM_WEB_PORT', '8921')}"
TTS_URL = os.getenv("TTS_API_URL", "https://whisper-asr.2dox.uz/speak")
TTS_TOKEN = os.getenv("ASR_TOKEN", "")
TTS_VOICE = "kseniya"
TTS_STYLE = "fast"

# How often to poll (seconds)
POLL_INTERVAL = 2.0
# Minimum new text length to trigger narration
MIN_NEW_TEXT = 50
# Don't narrate more often than this (seconds)
NARRATE_COOLDOWN = 5.0

last_output = ""
last_narrate_time = 0.0


def get_session_output() -> str:
    """Read current terminal output directly from tmux."""
    try:
        target = f"agent-{SESSION_ID}:0.0"
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target, "-S", "-"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return ""


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes."""
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'\x1b[^\x1b]{0,8}', '', text)
    return text


def strip_markdown(text: str) -> str:
    """Remove markdown formatting."""
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    return text


def is_noise(line: str) -> bool:
    """Check if a line is code/noise that should not be narrated."""
    t = line.strip()
    if not t or len(t) < 5:
        return True
    # Claude Code UI elements (tool calls, status lines, spinners)
    if re.search(r'[●○◐◑◒◓⏵⏸✓✗✻✽✾✿⎿╭╮╰╯│├└─❯❮▸▹►]', t):
        return True
    # Running/timeout/token/status indicators
    if re.search(r'Running|timeout|tokens|Brewing|Befuddling|bypass permissions|Auto-update|shift\+t', t, re.IGNORECASE):
        return True
    # Indented code
    if re.match(r'^\s{4,}\S', line):
        return True
    # Code fences, programming keywords
    if re.match(r'^(```|import |from |def |class |const |let |var |function |return |if \(|else\b|for \(|while \(|export |module\.)', t):
        return True
    # Line numbers, box drawing
    if re.match(r'^\s*\d+[→│|:\t]', t):
        return True
    if re.match(r'^[─━═╔╗╚╝├┤┬┴┼┌┐└┘│╭╮╰╯┃]', t):
        return True
    # Logs, file paths, shell commands
    if re.match(r'^(INFO|WARNING|ERROR|DEBUG|HTTP|Shell cwd)', t):
        return True
    if re.match(r'^[\/~][a-zA-Z0-9_\/.]+$', t):
        return True
    if re.match(r'^\$ ', t):
        return True
    # Tool call headers (Read, Write, Edit, Bash, Glob, Grep, etc.)
    if re.match(r'^(Read|Write|Edit|Bash|Glob|Grep|Agent|Skill)\(', t):
        return True
    # Pure symbols
    if not re.search(r'[а-яА-ЯёЁa-zA-Z]{2,}', t):
        return True
    return False


def extract_meaningful_text(new_text: str) -> str:
    """Extract human-readable text from terminal output diff."""
    clean = strip_ansi(new_text)
    clean = strip_markdown(clean)
    lines = clean.split('\n')
    good = [line.strip() for line in lines if not is_noise(line)]
    return ' '.join(good).strip()


def narrate_with_claude(text: str) -> str:
    """Use Claude CLI to generate a brief narration of the text."""
    prompt = (
        "Ты — Ксения, голосовой рассказчик для AI-терминала. "
        "Тебе дан фрагмент нового вывода из терминала, где работает AI-ассистент.\n\n"
        "ПРАВИЛА:\n"
        "- Если фрагмент содержит ЖИВУЮ РЕЧЬ ассистента (объяснения, ответы, рассуждения) — "
        "перескажи КРАТКО (1-3 предложения) от первого лица женского рода. Говори естественно.\n"
        "- Если фрагмент содержит ТОЛЬКО технический мусор (команды терминала, вывод инструментов, "
        "код, логи, статус-строки, пути к файлам) — ответь ОДНИМ словом: SKIP\n"
        "- НЕ озвучивай сообщения пользователя — только ответы ассистента.\n"
        "- Отвечай ТОЛЬКО текстом для озвучки (без markdown, без кавычек) или словом SKIP.\n\n"
        f"Фрагмент:\n{text[:2000]}"
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "claude-haiku-4-5-20251001"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"[NARRATOR] Claude error: {e}")

    return ""


def send_to_tts(text: str) -> str | None:
    """Send text to TTS API, return audio URL."""
    try:
        with httpx.Client(timeout=30.0, verify=False) as client:
            resp = client.post(TTS_URL, json={
                "text": text,
                "voice": TTS_VOICE,
                "style": TTS_STYLE,
                "token": TTS_TOKEN,
            })
            if resp.status_code == 200:
                data = resp.json()
                return data.get("url")
    except Exception as e:
        print(f"[NARRATOR] TTS error: {e}")
    return None


def push_audio(url: str):
    """Push audio URL to the main app for frontend playback."""
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(f"{BASE_URL}/narrator/{SESSION_ID}/push", json={"url": url})
    except Exception as e:
        print(f"[NARRATOR] Push error: {e}")


def main():
    global last_output, last_narrate_time

    print(f"[NARRATOR] Starting for session '{SESSION_ID}'")

    # Stabilize baseline: read twice with a pause to get consistent output
    last_output = get_session_output()
    time.sleep(1)
    last_output = get_session_output()
    last_narrate_time = time.time()

    while True:
        time.sleep(POLL_INTERVAL)

        current = get_session_output()
        if not current or current == last_output:
            continue

        # Find new content via common prefix
        i = 0
        min_len = min(len(last_output), len(current))
        while i < min_len and last_output[i] == current[i]:
            i += 1
        new_part = current[i:]
        last_output = current

        if len(new_part) < MIN_NEW_TEXT:
            continue

        # If diff is huge (>2000 chars), it's likely a scrollback dump after restart.
        # Only take the last ~500 chars (the actually new part).
        if len(new_part) > 2000:
            # Find a newline boundary near the end
            tail = new_part[-500:]
            nl = tail.find('\n')
            new_part = tail[nl + 1:] if nl >= 0 else tail
            if len(new_part) < MIN_NEW_TEXT:
                continue

        # Check cooldown
        now = time.time()
        if now - last_narrate_time < NARRATE_COOLDOWN:
            continue

        # Extract meaningful text
        meaningful = extract_meaningful_text(new_part)
        if not meaningful or len(meaningful) < 20:
            continue

        print(f"[NARRATOR] New text ({len(meaningful)} chars): {meaningful[:100]}...")

        # Generate narration with Claude
        narration = narrate_with_claude(meaningful)
        if not narration or narration.strip().upper() == "SKIP":
            print(f"[NARRATOR] Skipped (no speech content)")
            continue

        print(f"[NARRATOR] Narration: {narration[:100]}...")
        last_narrate_time = time.time()

        # Send to TTS
        audio_url = send_to_tts(narration)
        if not audio_url:
            continue

        # Push to frontend
        push_audio(audio_url)
        print(f"[NARRATOR] Audio pushed: {audio_url}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[NARRATOR] Stopped")
    except Exception as e:
        print(f"[NARRATOR] Fatal error: {e}")
        sys.exit(1)
