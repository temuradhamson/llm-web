#!/usr/bin/env bash
set -e

AGENT_HOME=/home/agent

mkdir -p "$AGENT_HOME/.claude" "$AGENT_HOME/.codex"

# Мерджим дефолтные файлы инсталлера в примонтированную конфиг-директорию
if [ -d /root/.claude-default ]; then
    cp -rn /root/.claude-default/. "$AGENT_HOME/.claude/" 2>/dev/null || true
fi

# Персистим .claude.json через симлинк в примонтированный том.
# Этот файл содержит oauthAccount, userID — без него CLI считает, что это fresh install.
SAVED="$AGENT_HOME/.claude/.root-claude.json"
if [ -f "$AGENT_HOME/.claude.json" ] && [ ! -L "$AGENT_HOME/.claude.json" ]; then
    mv "$AGENT_HOME/.claude.json" "$SAVED"
    ln -sf "$SAVED" "$AGENT_HOME/.claude.json"
elif [ ! -e "$AGENT_HOME/.claude.json" ] && [ -f "$SAVED" ]; then
    ln -sf "$SAVED" "$AGENT_HOME/.claude.json"
fi

# Права: agent владеет своим home и примонтированным конфигом
chown -R agent:agent "$AGENT_HOME" 2>/dev/null || true

# Всё остальное работает от agent (не root)
exec runuser -u agent -- bash -c '
    tmux start-server
    export WATCHFILES_FORCE_POLLING=true
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --reload --reload-dir /app/app
'
