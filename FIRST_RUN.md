# Первый запуск

## 1. Сборка образа

```bash
make build
```

## 2. Запуск контейнера

```bash
make run
make run WORKSPACE=/home/alexey-yakovenko/PythonProjects
```

Контейнер запустится в интерактивном режиме. Ты увидишь лог uvicorn — значит API работает.

## 3. Открыть второй терминал

Не закрывая первый терминал, открой новый и создай сессию:

```bash
curl -X POST http://localhost:8921/sessions \
  -H "Content-Type: application/json" \
  -d '{"session_id":"main", "workdir":"/workspace", "cli":"codex"}'
```

Ответ: `{"ok": true, "session_id": "main", ...}`

## 4. Подключиться к tmux и авторизоваться

```bash
make attach
```

Ты окажешься в терминале внутри контейнера. Дальше есть два сценария:

```bash
claude
```

или:

```bash
codex login
```

`Claude Code` запустит свою стандартную авторизацию.

`Codex` попросит войти через ChatGPT или использовать API key. После успешного входа он сохранит данные в `~/.codex-agent/` на хосте.

После успешного входа — отключись от tmux:

**`Ctrl+B`, затем `D`**

## 5. Проверить что всё работает

```bash
# Отправить промпт
curl -X POST http://localhost:8921/sessions/main/send \
  -H "Content-Type: application/json" \
  -d '{"text":"hello"}'

# Через пару секунд — прочитать ответ
curl -s http://localhost:8921/sessions/main/tail?lines=80
```

## 6. Готово

Конфиги авторизации сохранены в `~/.claude-agent/` и `~/.codex-agent/` на хосте. При следующих запусках шаг 4 не потребуется.

## Остановка

`Ctrl+C` в первом терминале или:

```bash
make stop
```
