import asyncio
import fcntl
import hashlib
import json as json_mod
import os
import pty
import re
import secrets
import select
import struct
import subprocess
import termios
from pathlib import Path
from enum import Enum
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
import httpx

from contextlib import asynccontextmanager
from datetime import datetime


# --- Persistence ---
DATA_DIR = Path(os.getenv("LLM_WEB_DATA", "/workspace/.llm_web_data"))
SESSIONS_FILE = DATA_DIR / "sessions.json"
HISTORY_DIR = DATA_DIR / "history"

SESSION_PREFIX = "agent"
STARTUP_TIMEOUT = 30
SEND_ENTER_DELAY = float(os.getenv("SEND_ENTER_DELAY", "0.2"))


# --- Auth ---
AUTH_FILE = DATA_DIR / "auth.json"
ACTIVE_SESSIONS: dict[str, str] = {}  # token -> username

PUBLIC_PATHS = {"/login", "/health"}


def hash_password(password: str, salt: str = "") -> str:
    if not salt:
        salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(password: str, stored: str) -> bool:
    salt = stored.split(":")[0]
    return hash_password(password, salt) == stored


def load_auth() -> dict:
    if AUTH_FILE.exists():
        try:
            return json_mod.loads(AUTH_FILE.read_text())
        except Exception:
            pass
    return {}


def save_auth(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json_mod.dumps(data, indent=2, ensure_ascii=False))


def ensure_default_auth():
    """Create default admin user if no auth file exists."""
    auth = load_auth()
    if not auth.get("users"):
        auth["users"] = {
            "admin": {"password": hash_password("админ123")}
        }
        save_auth(auth)


def get_user_from_request(request: Request) -> str | None:
    token = request.cookies.get("session_token")
    if token and token in ACTIVE_SESSIONS:
        return ACTIVE_SESSIONS[token]
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Allow public paths, static, and websocket (ws auth checked separately)
        if path in PUBLIC_PATHS or path.startswith("/ws/") or path.endswith("/push") or path.startswith("/narrator/"):
            # All narrator endpoints allowed (enable/disable/next check auth or localhost inside)
            return await call_next(request)

        user = get_user_from_request(request)
        if not user:
            if path.startswith("/sessions") or path.startswith("/asr") or path.startswith("/narrator"):
                return Response(status_code=401, content="Unauthorized")
            return RedirectResponse(url="/login", status_code=302)

        request.state.user = user
        return await call_next(request)


def ensure_data_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def load_session_registry() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json_mod.loads(SESSIONS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_session_registry(registry: dict):
    ensure_data_dirs()
    SESSIONS_FILE.write_text(json_mod.dumps(registry, indent=2, ensure_ascii=False))


def touch_session(session_id: str):
    """Update last_active timestamp for a session."""
    reg = load_session_registry()
    if session_id in reg:
        reg[session_id]["last_active"] = datetime.now().isoformat()
        save_session_registry(reg)


def register_session(session_id: str, workdir: str, cli: str):
    reg = load_session_registry()
    now = datetime.now().isoformat()
    reg[session_id] = {
        "workdir": workdir,
        "cli": cli,
        "created": now,
        "last_active": now,
    }
    save_session_registry(reg)


def unregister_session(session_id: str):
    reg = load_session_registry()
    reg.pop(session_id, None)
    save_session_registry(reg)


def save_history_snapshot(session_id: str):
    """Capture full terminal output and append new lines to history file."""
    target = f"{SESSION_PREFIX}-{session_id}:0.0"
    proc = run_cmd(["tmux", "capture-pane", "-p", "-t", target, "-S", "-"])
    if proc.returncode != 0:
        return
    ensure_data_dirs()
    history_file = HISTORY_DIR / f"{session_id}.log"
    current = proc.stdout
    # Write full snapshot (overwrite) — keeps latest state
    history_file.write_text(current)


async def history_saver_loop():
    """Background task: save history for all sessions every 60s."""
    while True:
        await asyncio.sleep(60)
        try:
            proc = run_cmd(["tmux", "list-sessions", "-F", "#{session_name}"])
            if proc.returncode != 0:
                continue
            for line in proc.stdout.strip().splitlines():
                if line.startswith(f"{SESSION_PREFIX}-"):
                    sid = line.removeprefix(f"{SESSION_PREFIX}-")
                    save_history_snapshot(sid)
        except Exception:
            pass


async def restore_sessions():
    """Restore sessions from registry on startup."""
    reg = load_session_registry()
    if not reg:
        return

    # Check which sessions are already running
    proc = run_cmd(["tmux", "list-sessions", "-F", "#{session_name}"])
    existing = set()
    if proc.returncode == 0:
        existing = {
            s.removeprefix(f"{SESSION_PREFIX}-")
            for s in proc.stdout.strip().splitlines()
            if s.startswith(f"{SESSION_PREFIX}-")
        }

    for sid, info in reg.items():
        if sid in existing:
            continue
        workdir = info.get("workdir", "/workspace/current")
        cli_name = info.get("cli", "claude")
        try:
            cli = CLI(cli_name)
        except ValueError:
            continue

        name = f"{SESSION_PREFIX}-{sid}"
        p = run_cmd(["tmux", "new-session", "-d", "-s", name, "-c", workdir])
        if p.returncode != 0:
            continue
        run_cmd(["tmux", "set-option", "-t", name, "history-limit", "50000"])
        target = f"{name}:0.0"
        run_cmd(["tmux", "send-keys", "-t", target, "-l", "--", CLI_COMMANDS[cli]])
        run_cmd(["tmux", "send-keys", "-t", target, "Enter"])
        print(f"[RESTORE] Session '{sid}' restored ({cli_name} in {workdir})")


def ensure_session_claude_md(session_id: str, workdir: str, cli: str):
    """Create CLAUDE.md in workdir if it doesn't exist, with session context."""
    wdir = Path(workdir)
    if not wdir.exists():
        return
    claude_md = wdir / "CLAUDE.md"
    if claude_md.exists():
        return
    claude_md.write_text(
        f"# Project Context\n\n"
        f"Session: {session_id}\n"
        f"CLI: {cli}\n"
        f"Working directory: {workdir}\n\n"
        f"## Notes\n\n"
        f"Add project-specific context here.\n"
    )


@asynccontextmanager
async def lifespan(app):
    """Startup: restore sessions + start history saver."""
    ensure_data_dirs()
    ensure_default_auth()
    await restore_sessions()
    task = asyncio.create_task(history_saver_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(AuthMiddleware)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

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


# --- Auth Endpoints ---

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = get_user_from_request(request)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")

    auth = load_auth()
    users = auth.get("users", {})
    user_data = users.get(username)

    if not user_data or not verify_password(password, user_data["password"]):
        return templates.TemplateResponse(request, "login.html", {"error": "Неверный логин или пароль"})

    token = secrets.token_hex(32)
    ACTIVE_SESSIONS[token] = username
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=86400 * 30)
    return response


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        ACTIVE_SESSIONS.pop(token, None)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/change-password")
async def change_password(request: Request, payload: ChangePasswordRequest):
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    auth = load_auth()
    user_data = auth["users"].get(user)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(payload.current_password, user_data["password"]):
        raise HTTPException(status_code=403, detail="Неверный текущий пароль")

    auth["users"][user]["password"] = hash_password(payload.new_password)
    save_auth(auth)
    return {"ok": True}


# --- Endpoints ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    resp = templates.TemplateResponse(request, "mobile.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/desktop", response_class=HTMLResponse)
def desktop(request: Request):
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

    # Sort by last_active (most recent first)
    reg = load_session_registry()
    sessions.sort(
        key=lambda sid: reg.get(sid, {}).get("last_active", reg.get(sid, {}).get("created", "")),
        reverse=True,
    )
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

    # Persist session + create CLAUDE.md
    register_session(payload.session_id, payload.workdir, payload.cli.value)
    ensure_session_claude_md(payload.session_id, payload.workdir, payload.cli.value)

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
    # Save final history before deleting
    save_history_snapshot(session_id)
    name = session_name(session_id)
    tmux("kill-session", "-t", name)
    unregister_session(session_id)
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


@app.get("/sessions/{session_id}/history")
def get_history(session_id: str):
    """Get saved history for a session (even if session is dead)."""
    validate_id(session_id)
    history_file = HISTORY_DIR / f"{session_id}.log"
    if history_file.exists():
        return {"session_id": session_id, "output": history_file.read_text()}
    return {"session_id": session_id, "output": ""}


@app.get("/sessions/registry")
def get_registry():
    """Get all registered sessions (including dead ones with history)."""
    reg = load_session_registry()
    # Check which are alive
    proc = tmux("list-sessions", "-F", "#{session_name}")
    alive = set()
    if proc.returncode == 0:
        alive = {
            s.removeprefix(f"{SESSION_PREFIX}-")
            for s in proc.stdout.strip().splitlines()
            if s.startswith(f"{SESSION_PREFIX}-")
        }
    result = {}
    for sid, info in reg.items():
        result[sid] = {**info, "alive": sid in alive}
    return result


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

    touch_session(session_id)
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
    # Auth check for WebSocket
    token = ws.cookies.get("session_token")
    if not token or token not in ACTIVE_SESSIONS:
        await ws.close(code=4001, reason="Unauthorized")
        return

    validate_id(session_id)
    name = session_name(session_id)

    check = tmux("has-session", "-t", name)
    if check.returncode != 0:
        await ws.close(code=4004, reason=f"session '{session_id}' not found")
        return

    await ws.accept()
    touch_session(session_id)

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


# --- TTS ---

TTS_API_URL = os.getenv("TTS_API_URL", "https://whisper-asr.2dox.uz/speak")


class TTSRequest(BaseModel):
    text: str
    voice: str = "kseniya"
    style: str = "fast"


@app.post("/tts")
async def tts(payload: TTSRequest):
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        resp = await client.post(
            TTS_API_URL,
            json={
                "text": payload.text,
                "voice": payload.voice,
                "style": payload.style,
                "token": ASR_TOKEN,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"TTS error: {resp.text[:200]}")

    data = resp.json()
    return {"ok": True, "url": data.get("url", "")}


# --- Narrator (hook-driven TTS) ---
#
# Flow: Claude Stop hook → POST /narrator/hook (with assistant text)
#       → summarize text → TTS API → queue audio blob
#       → frontend polls /narrator/{session}/next → plays WAV
#
# No more tmux polling or Claude Haiku subprocess.

# In-memory state: session_id -> {"enabled": bool, "queue": [bytes]}
NARRATOR_STATE: dict[str, dict] = {}


def _narrator_summarize(text: str) -> str:
    """Create a brief spoken summary from assistant response text.

    Strips code blocks, tool output, and keeps only human-readable parts.
    Returns text suitable for TTS (1-3 sentences).
    """
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code
    text = re.sub(r'`[^`]+`', '', text)
    # Remove markdown headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    # Remove file paths
    text = re.sub(r'[/~][\w./\-]+\.\w+', '', text)
    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)
    # Remove lines that look like code or tool output
    lines = text.split('\n')
    good = []
    for line in lines:
        s = line.strip()
        if not s or len(s) < 10:
            continue
        # Skip indented lines (code)
        if line.startswith('    ') or line.startswith('\t'):
            continue
        # Skip lines with mostly special chars
        alpha = sum(1 for c in s if c.isalpha() or c == ' ')
        if alpha < len(s) * 0.5:
            continue
        good.append(s)

    result = ' '.join(good).strip()
    # Truncate to ~500 chars for TTS (about 30 seconds of speech)
    if len(result) > 500:
        # Cut at sentence boundary
        cut = result[:500]
        last_dot = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
        if last_dot > 200:
            result = cut[:last_dot + 1]
        else:
            result = cut + '...'
    return result


def _narrator_check_auth(request: Request):
    """Allow if authenticated user OR localhost."""
    user = get_user_from_request(request)
    if user:
        return
    client_host = request.client.host if request.client else ""
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/narrator/{session_id}/enable")
async def narrator_enable(session_id: str, request: Request):
    _narrator_check_auth(request)
    validate_id(session_id)
    state = NARRATOR_STATE.setdefault(session_id, {"enabled": False, "queue": []})
    state["enabled"] = True
    state["queue"] = []
    print(f"[NARRATOR] Enabled for session '{session_id}'")
    return {"ok": True, "status": "enabled"}


@app.post("/narrator/{session_id}/disable")
async def narrator_disable(session_id: str, request: Request):
    _narrator_check_auth(request)
    validate_id(session_id)
    state = NARRATOR_STATE.get(session_id)
    if state:
        state["enabled"] = False
        state["queue"] = []
    print(f"[NARRATOR] Disabled for session '{session_id}'")
    return {"ok": True, "status": "disabled"}


@app.get("/narrator/{session_id}/next")
async def narrator_next(session_id: str, request: Request):
    """Frontend polls this — returns audio WAV if available."""
    _narrator_check_auth(request)
    validate_id(session_id)
    state = NARRATOR_STATE.get(session_id)
    if not state or not state["queue"]:
        return Response(
            content=json_mod.dumps({"url": None}),
            media_type="application/json",
        )
    audio_bytes = state["queue"].pop(0)
    print(f"[NARRATOR] Serving audio to frontend ({len(audio_bytes)} bytes)")
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={"X-Narrator": "true"},
    )


class NarratorHookPayload(BaseModel):
    text: str
    claude_session_id: str = ""
    brief: bool = False  # True for real-time tool updates (skip summarize)


@app.post("/narrator/hook")
async def narrator_hook(payload: NarratorHookPayload, request: Request):
    """Called by Claude Stop hook — receives assistant response text, generates TTS."""
    # Only allow from localhost
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="localhost only")
    text = payload.text.strip()
    if not text or len(text) < 20:
        return {"ok": False, "detail": "text too short"}

    # Find any enabled narrator session to push audio to
    enabled_sessions = [sid for sid, st in NARRATOR_STATE.items() if st.get("enabled")]
    if not enabled_sessions:
        return {"ok": False, "detail": "no narrator sessions enabled"}

    # Brief updates (from PostToolUse) skip summarization — already short and ready
    if payload.brief:
        summary = text
        print(f"[NARRATOR] Brief: {summary}")
    else:
        # Summarize long text for speech (Stop hook)
        summary = _narrator_summarize(text)
        if not summary or len(summary) < 10:
            print(f"[NARRATOR] Hook: text too short after summarize ({len(summary)} chars)")
            return {"ok": False, "detail": "summary too short"}
        print(f"[NARRATOR] Summary: {len(summary)} chars: {summary[:100]}...")

    # Send to TTS API
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            resp = await client.post(
                TTS_API_URL,
                json={
                    "text": summary,
                    "voice": "kseniya",
                    "style": "fast",
                    "token": ASR_TOKEN,
                },
            )
            if resp.status_code != 200:
                print(f"[NARRATOR] TTS API error: {resp.status_code} {resp.text[:200]}")
                return {"ok": False, "detail": "TTS API error"}

            tts_data = resp.json()
            audio_url = tts_data.get("url", "")
            if not audio_url:
                print("[NARRATOR] TTS API returned no URL")
                return {"ok": False, "detail": "no audio URL"}

            # Download the audio and store as bytes (avoids CORS/proxy issues)
            audio_resp = await client.get(audio_url)
            if audio_resp.status_code != 200:
                print(f"[NARRATOR] Audio download error: {audio_resp.status_code}")
                return {"ok": False, "detail": "audio download error"}

            audio_bytes = audio_resp.content
            print(f"[NARRATOR] Audio ready: {len(audio_bytes)} bytes from {audio_url}")

    except Exception as e:
        print(f"[NARRATOR] TTS error: {e}")
        return {"ok": False, "detail": str(e)}

    # Push audio to all enabled sessions
    for sid in enabled_sessions:
        st = NARRATOR_STATE.get(sid)
        if st and st.get("enabled"):
            st["queue"].append(audio_bytes)
            print(f"[NARRATOR] Pushed audio to session '{sid}'")

    return {"ok": True, "summary": summary[:100]}


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
