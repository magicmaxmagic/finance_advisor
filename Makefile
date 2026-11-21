# finlab

SHELL := /bin/bash
.DEFAULT_GOAL := help

HOST ?= 127.0.0.1
API_PORT ?= 8000
WEB_PORT ?= 5173
AS_OF ?= $(shell date +%F)
START ?= 1996-01-01
END ?= $(shell date +%F)
DATA_DIR ?= data
RUNTIME_DIR ?= $(CURDIR)/.finlab

.PHONY: help setup env ingest prices-yahoo universe submissions concepts metrics \
        engine-publish test lint fmt frontend-build check start stop clean clean-data

help: ## Show commands
	@awk 'BEGIN {FS=":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install dependencies and initialize the database
	uv sync --all-extras
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.cli db init

env: ## Create .env from the example
	@if [ ! -f .env ]; then cp .env.example .env; echo "created .env"; fi

ingest: ## Ingest SEC PoC universe
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.cli ingest poc

prices-yahoo: ## Backfill Yahoo OHLCV
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.cli ingest prices-yahoo --start $(START) --end $(END)

universe: ## Capture today's universe snapshot
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.cli ingest universe

submissions: ## Backfill filing metadata
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.cli ingest submissions

concepts: ## Load concepts and derive Q4
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.cli concepts load

metrics: ## Recompute metrics
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.cli metrics --as-of $(AS_OF)

engine-publish: ## Publish a warehouse snapshot
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.engine.cli publish

start: ## Start engine, Analytics API and React frontend
	@mkdir -p $(RUNTIME_DIR)/logs
	@nohup env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.daemon.main > $(RUNTIME_DIR)/logs/engine.log 2>&1 & echo $$! > $(RUNTIME_DIR)/engine.pid
	@nohup env PYTHONPATH=$(CURDIR)/src uv run --no-sync python -m finlab.analytics.cli serve --host $(HOST) --port $(API_PORT) > $(RUNTIME_DIR)/logs/analytics.log 2>&1 & echo $$! > $(RUNTIME_DIR)/analytics.pid
	@cd frontend && nohup npm run dev -- --host $(HOST) --port $(WEB_PORT) > $(RUNTIME_DIR)/logs/frontend.log 2>&1 & echo $$! > $(RUNTIME_DIR)/frontend.pid
	@echo "FinLab starting:"
	@echo "  API       http://$(HOST):$(API_PORT)"
	@echo "  Frontend  http://$(HOST):$(WEB_PORT)"

stop: ## Stop engine, Analytics API and React frontend
	@pids=""; \
	for file in $(RUNTIME_DIR)/engine.pid $(RUNTIME_DIR)/analytics.pid $(RUNTIME_DIR)/frontend.pid; do \
		if [ -f "$$file" ]; then pids="$$pids $$(cat "$$file")"; rm -f "$$file"; fi; \
	done; \
	for port in $(API_PORT) $(WEB_PORT); do \
		found="$$(lsof -ti tcp:"$$port" -sTCP:LISTEN 2>/dev/null)"; \
		pids="$$pids $$found"; \
	done; \
	pids="$$(echo $$pids | xargs -n1 2>/dev/null | sort -u | xargs 2>/dev/null)"; \
	if [ -n "$$pids" ]; then kill $$pids 2>/dev/null || true; echo "FinLab stopped"; else echo "FinLab is not running"; fi

status: ## Show service listeners and process PIDs
	@for file in $(RUNTIME_DIR)/engine.pid $(RUNTIME_DIR)/analytics.pid $(RUNTIME_DIR)/frontend.pid; do \
		if [ -f "$$file" ]; then echo "$$file $$(cat "$$file")"; fi; \
	done
	@lsof -nP -iTCP:$(API_PORT) -iTCP:$(WEB_PORT) -sTCP:LISTEN 2>/dev/null || true

test: ## Run backend tests
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync pytest

lint: ## Run Python lint and format checks
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync ruff check .
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync ruff format --check .

fmt: ## Format Python code
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync ruff format .
	env PYTHONPATH=$(CURDIR)/src uv run --no-sync ruff check --fix .

frontend-build: ## Build and lint React
	cd frontend && npm run build
	cd frontend && npm run lint

check: lint test frontend-build ## Run CI-style validation

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache dist build htmlcov .coverage frontend/dist frontend/node_modules/.cache

clean-data: ## Delete database and warehouse, keeping raw downloads
	rm -rf $(DATA_DIR)/warehouse $(DATA_DIR)/db $(DATA_DIR)/staging
