IMAGE_NAME = ai-agent-box
PORT = 8921
WORKSPACE ?= $(HOME)/PythonProjects/codex_test
CLAUDE_CONFIG ?= $(HOME)/.claude-agent
CODEX_CONFIG ?= $(HOME)/.codex-agent
.PHONY: build run start stop restart health logs attach

build:
	docker build --network host -t $(IMAGE_NAME) .

run:
	@mkdir -p "$(CLAUDE_CONFIG)" "$(CODEX_CONFIG)"
	docker run --rm -it \
		--network host \
		--hostname $(IMAGE_NAME) \
		\
		-v "$(WORKSPACE):/workspace" \
		-v "$(CLAUDE_CONFIG):/home/agent/.claude" \
		-v "$(CODEX_CONFIG):/home/agent/.codex" \
		-v "$(CURDIR)/app:/app/app" \
		-v "$(CURDIR)/entrypoint.sh:/app/entrypoint.sh" \
		--name $(IMAGE_NAME) \
		$(IMAGE_NAME)

start:
	@mkdir -p "$(CLAUDE_CONFIG)" "$(CODEX_CONFIG)"
	docker run -d \
		--network host \
		--restart unless-stopped \
		--hostname $(IMAGE_NAME) \
		\
		-v "$(WORKSPACE):/workspace" \
		-v "$(CLAUDE_CONFIG):/home/agent/.claude" \
		-v "$(CODEX_CONFIG):/home/agent/.codex" \
		-v "$(CURDIR)/app:/app/app" \
		-v "$(CURDIR)/entrypoint.sh:/app/entrypoint.sh" \
		--name $(IMAGE_NAME) \
		$(IMAGE_NAME)

attach:
	docker exec -it -u agent $(IMAGE_NAME) tmux attach

stop:
	docker stop $(IMAGE_NAME) 2>/dev/null || true
	docker rm $(IMAGE_NAME) 2>/dev/null || true

restart:
	docker restart $(IMAGE_NAME)

health:
	curl -s http://localhost:$(PORT)/health | python3 -m json.tool

logs:
	docker logs -f $(IMAGE_NAME)
