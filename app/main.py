import asyncio
import fcntl
import json as json_mod
import os
import pty
import re
import select
import struct
import subprocess
import termios
from pathlib import Path
from enum import Enum
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx

app = FastAPI()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESSION_PREFIX = "agent"
STARTUP_TIMEOUT = 30
SEND_ENTER_DELAY = float(os.getenv("SEND_ENTER_DELAY", "0.2"))

# ASR config
ASR_API_URL = os.getenv("ASR_API_URL", "https://whisper-asr.2dox.uz/qwen/transcribe")
ASR_TOKEN = os.getenv("ASR_TOKEN", "")
ASR_LANGUAGE = os.getenv("ASR_LANGUAGE", "ru")


class CLI(str, Enum):
    claude = "claude"
    gemini = "gemini"
    codex = "codex"
    qwen = "qwen"


CLI_COMMANDS: dict[CLI, str] = {
    CLI.claude: "claude --dangerously-skip-permissions",
    CLI.gemini: "gemini",
    CLI.codex: "codex --ask-for-approval never --sandbox danger-full-access",
    CLI.qwen: "qwen",
}

CLI_READY_MARKERS: dict[CLI, list[str]] = {
    CLI.claude: [">", "❯"],
    CLI.gemini: [">", "❯"],
    CLI.codex: [">", "❯"],
    CLI.qwen: [">", "❯"],
}

def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def tmux(*args: str) -> subprocess.CompletedProcess:
    return run_cmd(["tmux", *args])


def session_name(session_id: str) -> str:
    return f"{SESSION_PREFIX}-{session_id}"


def session_target(session_id: str) -> str:
    return f"{session_name(session_id)}:0.0"


def get_pane_output(session_id: str, lines: int = 20) -> str:
    proc = tmux("capture-pane", "-p", "-t", session_target(session_id), "-S", f"-{lines}")
    return proc.stdout if proc.returncode == 0 else ""


def validate_id(session_id: str):
    if not re.match(r"^[a-zA-Z0-9_-]+$", session_id):
        raise HTTPException(status_code=400, detail="session_id must be alphanumeric, dash, or underscore")


def require_session(session_id: str):
    validate_id(session_id)
    name = session_name(session_id)
    proc = tmux("has-session", "-t", name)
    if proc.returncode != 0:
        raise HTTPException(status_code=404, detail=f"session '{session_id}' not found :()")


async def wait_for_ready(session_id: str, cli: CLI) -> bool:
    """Ждём появления prompt-маркера."""
    markers = CLI_READY_MARKERS[cli]
    for _ in range(STARTUP_TIMEOUT * 2):
        output = get_pane_output(session_id, lines=40)
        if any(m in output for m in markers):
            return True
        await asyncio.sleep(1)
    return False


# --- Models ---

class CreateSession(BaseModel):
    session_id: str
    workdir: str = "/workspace/current"
    cli: CLI = CLI.claude


class SendRequest(BaseModel):
    text: str


# --- Endpoints ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    resp = templates.TemplateResponse(request, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/mobile", response_class=HTMLResponse)
def mobile(request: Request):
    resp = templates.TemplateResponse(request, "mobile.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/health")
def health():
    proc = tmux("list-sessions", "-F", "#{session_name}")
    sessions = [
        s.removeprefix(f"{SESSION_PREFIX}-")
        for s in proc.stdout.strip().splitlines()
        if s.startswith(f"{SESSION_PREFIX}-")
    ] if proc.returncode == 0 else []
    return {"ok": True, "sessions": sessions}


@app.post("/sessions")
async def create_session(payload: CreateSession):
    validate_id(payload.session_id)
    name = session_name(payload.session_id)

    check = tmux("has-session", "-t", name)
    if check.returncode == 0:
        raise HTTPException(status_code=409, detail=f"session '{payload.session_id}' already exists")

    proc = tmux("new-session", "-d", "-s", name, "-c", payload.workdir)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=proc.stderr.strip() or "failed to create session")

    tmux("set-option", "-t", name, "history-limit", "50000")

    target = session_target(payload.session_id)
    tmux("send-keys", "-t", target, "-l", "--", CLI_COMMANDS[payload.cli])
    tmux("send-keys", "-t", target, "Enter")

    ready = await wait_for_ready(payload.session_id, payload.cli)
    if not ready:
        output = get_pane_output(payload.session_id, lines=40)
        return {
            "ok": False,
            "session_id": payload.session_id,
            "cli": payload.cli.value,
            "workdir": payload.workdir,
            "detail": f"{payload.cli.value} started but not ready within timeout",
            "output": output,
        }

    return {
        "ok": True,
        "session_id": payload.session_id,
        "cli": payload.cli.value,
        "workdir": payload.workdir,
        "status": "ready",
    }


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    require_session(session_id)
    name = session_name(session_id)
    tmux("kill-session", "-t", name)
    return {"ok": True, "session_id": session_id}


@app.get("/sessions/{session_id}/tail")
def tail(session_id: str, lines: int = 80, full: bool = False):
    require_session(session_id)
    target = session_target(session_id)
    start = "-" if full else f"-{lines}"
    proc = tmux("capture-pane", "-p", "-e", "-t", target, "-S", start)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=proc.stderr.strip() or "capture failed")
    return {"session_id": session_id, "output": proc.stdout}


@app.post("/sessions/{session_id}/send")
async def send(session_id: str, payload: SendRequest):
    require_session(session_id)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")

    target = session_target(session_id)

    send_text = tmux("send-keys", "-t", target, "-l", "--", text)
    if send_text.returncode != 0:
        raise HTTPException(status_code=500, detail=send_text.stderr.strip() or "send text failed")

    # Codex can treat rapid paste+Enter as an unfinished input line, so give tmux a moment
    # to flush the text before submitting it.
    await asyncio.sleep(SEND_ENTER_DELAY)

    send_enter = tmux("send-keys", "-t", target, "Enter")
    if send_enter.returncode != 0:
        raise HTTPException(status_code=500, detail=send_enter.stderr.strip() or "send enter failed")

    return {"ok": True, "session_id": session_id, "sent": text}


@app.post("/sessions/{session_id}/interrupt")
def interrupt(session_id: str):
    require_session(session_id)
    target = session_target(session_id)
    tmux("send-keys", "-t", target, "C-c")
    return {"ok": True, "session_id": session_id}


# --- WebSocket terminal ---

@app.websocket("/ws/terminal/{session_id}")
async def ws_terminal(ws: WebSocket, session_id: str):
    """WebSocket — подключает xterm.js к tmux-сессии через pty."""
    validate_id(session_id)
    name = session_name(session_id)

    check = tmux("has-session", "-t", name)
    if check.returncode != 0:
        await ws.close(code=4004, reason=f"session '{session_id}' not found")
        return

    await ws.accept()

    tmux("set-option", "-t", name, "aggressive-resize", "on")
    tmux("set-option", "-t", name, "status", "off")

    master_fd, slave_fd = pty.openpty()

    # Ждём первый resize от клиента, чтобы сразу задать правильный размер PTY.
    # Это избавляет от перерисовки tmux при подключении.
    cols, rows = 120, 40
    try:
        msg = await asyncio.wait_for(ws.receive_text(), timeout=3.0)
        if msg.startswith('{"type":"resize"'):
            data = json_mod.loads(msg)
            cols = data.get("cols", 120)
            rows = data.get("rows", 40)
    except (asyncio.TimeoutError, Exception):
        pass

    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))

    proc = subprocess.Popen(
        ["tmux", "attach-session", "-d", "-t", name],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave_fd)

    loop = asyncio.get_event_loop()

    async def read_pty():
        """Читаем вывод из pty и отправляем в WebSocket (event-driven + coalescing)."""
        try:
            while True:
                # Ждём данных через event loop (не polling)
                readable = loop.create_future()
                loop.add_reader(master_fd, readable.set_result, None)
                try:
                    await readable
                finally:
                    loop.remove_reader(master_fd)

                # Короткая пауза для склейки мелких чанков в одно сообщение
                await asyncio.sleep(0.005)

                # Вычитываем всё доступное
                data = b""
                while select.select([master_fd], [], [], 0)[0]:
                    try:
                        chunk = os.read(master_fd, 65536)
                        if not chunk:
                            if data:
                                await ws.send_bytes(data)
                            return
                        data += chunk
                    except OSError:
                        if data:
                            await ws.send_bytes(data)
                        return

                if data:
                    await ws.send_bytes(data)
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    async def write_pty():
        """Читаем ввод из WebSocket и пишем в pty."""
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break

                if "text" in msg:
                    text = msg["text"]
                    if text.startswith('{"type":"resize"'):
                        data = json_mod.loads(text)
                        c = data.get("cols", 80)
                        r = data.get("rows", 24)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", r, c, 0, 0))
                    else:
                        os.write(master_fd, text.encode())
                elif "bytes" in msg:
                    os.write(master_fd, msg["bytes"])
        except (WebSocketDisconnect, asyncio.CancelledError, Exception):
            pass

    try:
        await asyncio.gather(read_pty(), write_pty())
    except asyncio.CancelledError:
        pass
    finally:
        proc.terminate()
        try:
            os.close(master_fd)
        except OSError:
            pass


# --- ASR ---

@app.post("/asr")
async def asr(audio: UploadFile = File(...)):
    import tempfile
    audio_bytes = await audio.read()
    filename = audio.filename or "audio.wav"
    print(f"[ASR] Received {len(audio_bytes)} bytes, filename={filename}, content_type={audio.content_type}")

    # Convert to WAV via ffmpeg for maximum ASR compatibility
    wav_bytes = audio_bytes
    if not filename.endswith(".wav"):
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{filename.rsplit('.', 1)[-1]}", delete=False) as src:
                src.write(audio_bytes)
                src_path = src.name
            dst_path = src_path + ".wav"
            proc = subprocess.run(
                ["ffmpeg", "-y", "-i", src_path, "-ar", "16000", "-ac", "1", "-f", "wav", dst_path],
                capture_output=True, timeout=30,
            )
            if proc.returncode == 0:
                wav_bytes = Path(dst_path).read_bytes()
                print(f"[ASR] Converted to WAV: {len(wav_bytes)} bytes")
            else:
                print(f"[ASR] ffmpeg failed: {proc.stderr.decode()[-200:]}")
            Path(src_path).unlink(missing_ok=True)
            Path(dst_path).unlink(missing_ok=True)
        except Exception as e:
            print(f"[ASR] Conversion error: {e}")

    async with httpx.AsyncClient(timeout=600.0, verify=False) as client:
        resp = await client.post(
            ASR_API_URL,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={
                "language": ASR_LANGUAGE,
                "with_normalize": "false",
                "token": ASR_TOKEN,
            },
        )

    print(f"[ASR] Response: status={resp.status_code}, body={resp.text[:300]}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ASR error: {resp.text}")

    return resp.json()


# --- SSL Certificate download (for iOS mic fix) ---

@app.get("/ssl-cert")
def download_cert():
    cert_path = Path(__file__).parent / "templates" / "llm-web.crt"
    cert_data = cert_path.read_bytes()
    return Response(
        content=cert_data,
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": "attachment; filename=llm-web.crt"},
    )
