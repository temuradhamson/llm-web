# Claude Code + Codex in Docker — MVP

Веб-доступ к CLI-агентам через FastAPI + tmux в Docker-контейнере.
Сейчас поддерживаются `Claude Code` и `Codex`, включая несколько параллельных терминалов и сохранение логина между перезапусками.

## Архитектура

```
Browser / curl  →  FastAPI (backend)  →  tmux send-keys  →  Agent CLI
                   :8921                                     (Claude / Codex / ...)
```

## Быстрый старт

### 1. Сборка и запуск

```bash
make build
make run
```

Или вручную:

```bash
docker build --network host -t ai-agent-box .

docker run --rm -it \
  --network host \
  -v "$HOME/projects:/workspace" \
  -v "$HOME/.claude-agent:/home/agent/.claude" \
  -v "$HOME/.codex-agent:/home/agent/.codex" \
  ai-agent-box
```

### 2. Первый запуск — авторизация

При первом запуске можно один раз залогиниться в нужный CLI:

```bash
# Claude
curl -X POST http://localhost:8921/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id":"claude-main", "workdir":"/workspace", "cli":"claude"}'

# Codex
curl -X POST http://localhost:8921/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id":"codex-main", "workdir":"/workspace", "cli":"codex"}'

# Подключиться к терминалу
make attach
# или: docker exec -it ai-agent-box tmux attach

# В терминале:
# - для Claude пройти стандартную авторизацию Claude Code
# - для Codex выбрать "Sign in with ChatGPT" или выполнить `codex login`
# Отключиться: Ctrl+B, D
```

Конфиги сохраняются на хосте в `~/.claude-agent/` и `~/.codex-agent/`. При следующих запусках повторная авторизация обычно не требуется.

### 3. Работа через API

```bash
# Создать сессию с указанием проекта
curl -X POST http://localhost:8921/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id":"main", "workdir":"/workspace/my-project", "cli":"codex"}'

# Отправить промпт
curl -X POST http://localhost:8921/sessions/main/send \
  -H "Content-Type: application/json" \
  -d '{"text":"посмотри структуру проекта"}'

# Прочитать вывод
curl http://localhost:8921/sessions/main/tail?lines=120

# Прервать выполнение (Ctrl+C)
curl -X POST http://localhost:8921/sessions/main/interrupt
```

## API

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | `/health` | Список активных сессий |
| POST | `/sessions` | Создать сессию. Body: `{"session_id":"...", "workdir":"...", "cli":"claude|codex|..."}` |
| DELETE | `/sessions/{id}` | Удалить сессию |
| POST | `/sessions/{id}/send` | Отправить текст. Body: `{"text":"..."}` |
| GET | `/sessions/{id}/tail?lines=N` | Последние N строк вывода (по умолчанию 80) |
| POST | `/sessions/{id}/interrupt` | Отправить Ctrl+C |

## Несколько проектов

Монтируй корневую папку с проектами и указывай workdir при создании сессии:

```bash
# Сессия для проекта A
curl -X POST http://localhost:8921/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id":"proj-a", "workdir":"/workspace/project-a"}'

# Сессия для проекта B
curl -X POST http://localhost:8921/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id":"proj-b", "workdir":"/workspace/project-b"}'
```

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `PORT` | `8921` | Порт FastAPI |

## Bind mounts

| Хост | Контейнер | Назначение |
|------|-----------|------------|
| `~/.claude-agent/` | `/home/agent/.claude` | Конфиг и авторизация Claude Code |
| `~/.codex-agent/` | `/home/agent/.codex` | Конфиг, `auth.json` и история Codex |
| `~/projects/` | `/workspace` | Проекты для работы |
