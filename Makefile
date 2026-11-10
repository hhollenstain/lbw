.PHONY: sync test lint doctor run watch certs up-local restart-local restart-agent restart-crossing wipe export-github hydrate up-mac up-nvidia pull-model openapi openapi-check

WORKSPACE ?= examples/demo-project
GOAL ?= Make the test suite pass.
PORT ?= 8080
LBW_POSTGRES_PORT ?= 5432
LBW_KIWIFS_PORT ?= 3333
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.mac.yml
COMPOSE_ENV := LBW_UI_PORT=$(PORT) LBW_POSTGRES_PORT=$(LBW_POSTGRES_PORT) LBW_KIWIFS_PORT=$(LBW_KIWIFS_PORT)

sync:
	uv sync --extra dev

web:
	cd web && npm ci && npm run build

openapi:
	uv run python scripts/openapi_models.py
	uv run ruff format src/agent_lbw/openapi_models.py

openapi-check:
	uv run python scripts/openapi_models.py --check

web-check:
	cd web && npm ci
	rm -rf .web-check
	cd web && LBW_WEB_OUT="$(CURDIR)/.web-check" npm run build
	diff -rq .web-check src/agent_lbw/static/app
	rm -rf .web-check

test:
	uv run pytest -q

lint:
	uv run ruff format src tests libs services
	uv run ruff check src tests libs services

doctor:
	uv run lbw doctor

run:
	uv run lbw run --workspace $(WORKSPACE) --goal "$(GOAL)"

watch:
	uv run lbw watch --workspace $(WORKSPACE)

certs:
	bash scripts/local-certs.sh

up-local:
	test -f .lbw/certs/server.crt || $(MAKE) certs
	$(COMPOSE_ENV) $(COMPOSE) build agent crossing
	$(COMPOSE_ENV) $(COMPOSE) up -d --wait postgres
	$(COMPOSE_ENV) $(COMPOSE) up -d kiwifs
	$(COMPOSE_ENV) $(COMPOSE) up -d --no-deps agent crossing ingress

restart-local:
	$(COMPOSE_ENV) $(COMPOSE) up -d --build --no-deps --force-recreate agent crossing
	$(COMPOSE_ENV) $(COMPOSE) up -d --no-deps ingress

restart-agent:
	$(COMPOSE_ENV) $(COMPOSE) up -d --build --no-deps --force-recreate agent
	$(COMPOSE_ENV) $(COMPOSE) up -d --no-deps ingress

restart-crossing:
	$(COMPOSE_ENV) $(COMPOSE) up -d --build --no-deps --force-recreate crossing
	$(COMPOSE_ENV) $(COMPOSE) up -d --no-deps ingress

wipe:
	$(COMPOSE_ENV) $(COMPOSE) down --volumes --remove-orphans
	$(COMPOSE_ENV) docker compose -f docker-compose.yml -f docker-compose.nvidia.yml down --volumes --remove-orphans

export-github:
	LBW_DATABASE_URL=postgresql://lbw:lbw@127.0.0.1:$(LBW_POSTGRES_PORT)/lbw \
		uv run lbw export-github --workspace workspaces --file github.local.yaml

hydrate:
	LBW_DATABASE_URL=postgresql://lbw:lbw@127.0.0.1:$(LBW_POSTGRES_PORT)/lbw \
		uv run lbw hydrate --workspace workspaces --file github.local.yaml

up-mac:
	docker compose -f docker-compose.yml -f docker-compose.mac.yml up --build

up-nvidia:
	docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up --build

pull-model:
	ollama pull $(or $(MODEL),qwen2.5-coder:14b)
