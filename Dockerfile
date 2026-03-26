FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/root \
    PORT=8921

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    git \
    tmux \
    ca-certificates \
    locales \
    && locale-gen en_US.UTF-8 ru_RU.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update && apt-get install -y nodejs \
    && npm install -g @openai/codex \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://claude.ai/install.sh | bash

# Сохраняем дефолтные файлы инсталлера для мерджа при старте
RUN cp -a /root/.claude /root/.claude-default

# Не-root пользователь (--dangerously-skip-permissions требует не-root).
# ubuntu уже есть с uid=1000 — переименовываем и даём home.
RUN usermod -l agent -d /home/agent -m ubuntu && groupmod -n agent ubuntu

# Копируем бинарник Claude в глобальный путь (симлинк не работает — /root закрыт)
RUN cp "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude

ENV PATH="/usr/local/bin:${PATH}" \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    CODEX_HOME=/home/agent/.codex

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY app /app/app
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8921

CMD ["/app/entrypoint.sh"]
