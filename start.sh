#!/bin/bash
# llm_web WSL start script

export PATH="/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin"
export PATH="$PATH:/home/xtech/.local/bin"
export PATH="$PATH:/mnt/c/Users/xtech/AppData/Roaming/npm"
export PATH="$PATH:/mnt/c/Program Files/nodejs"
export HOME="/home/xtech"
export USER="xtech"
export DOCKER_HOST="unix:///var/run/docker.sock"
export CLAUDE_CONFIG_DIR="/home/xtech/.claude"
export SEND_ENTER_DELAY="0.3"

cd /mnt/c/Users/xtech/Projects/llm_web
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8921
