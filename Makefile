# finlab — FinLab Data Engine + FinLab Analytics

SHELL := /bin/bash
.DEFAULT_GOAL := help

HOST ?= 127.0.0.1
API_PORT ?= 8000
WEB_PORT ?= 5173
AS_OF ?= $(shell date +%F)
START ?= 1996-01-01
END ?= $(shell date +%F)
DATA_DIR ?= data

.PHONY: help setup env ingest prices-yahoo universe submissions concepts metrics \
        engine engine-status engine-publish analytics analytics-status web app \
        test lint fmt frontend-test frontend-build check stop clean clean-data

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- setup and data engine -----------------------------------------------

setup: ## Install Python dependencies and initialize the database
	uv sync --all-extras
	uv run finlab db init

env: ## Create .env from the example
	@if [ ! -f .env ]; then cp .env.example .env; echo "created .env"; fi

ingest: ## Ingest the fixed SEC PoC universe
	uv run finlab ingest poc

prices-yahoo: ## Backfill Yahoo raw OHLCV
	uv run finlab ingest prices-yahoo --start $(START) --end $(END)

universe: ## Capture today's historical universe snapshot
	uv run finlab ingest universe

submissions: ## Backfill SEC filing acceptance metadata
	uv run finlab ingest submissions

concepts: ## Load canonical concepts and derive Q4
	uv run finlab concepts load

metrics: ## Recompute metrics for AS_OF
	uv run finlab metrics --as-of $(AS_OF)

engine: ## Run the Data Engine daemon (single writer)
	uv run finlabd

engine-status: ## Show the latest warehouse publication
	uv run finlab-engine status

engine-publish: ## Publish a versioned Analytics warehouse snapshot
	uv run finlab-engine publish

# --- analytics and frontend ---------------------------------------------

analytics-status: ## Validate the warehouse snapshot contract
	uv run finlab-analytics status

analytics: ## Run read-only Analytics FastAPI
	uv run finlab-analytics serve --host $(HOST) --port $(API_PORT)

web: ## Run the React/Vite frontend
	cd frontend && npm run dev -- --host $(HOST) --port $(WEB_PORT)

app: ## Run Analytics FastAPI and React together
	@uv run finlab-analytics serve --host $(HOST) --port $(API_PORT) & \
	api_pid=$$!; \
	trap 'kill $$api_pid 2>/dev/null || true' EXIT INT TERM; \
	cd frontend && npm run dev -- --host $(HOST) --port $(WEB_PORT)

# --- validation ---------------------------------------------------------

test: ## Run backend tests
	uv run pytest

lint: ## Run backend lint and format checks
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Format backend code
	uv run ruff format .
	uv run ruff check --fix .

frontend-test: ## Run frontend tests (when configured)
	cd frontend && npm run test --if-present

frontend-build: ## Build and lint the React frontend
	cd frontend && npm run build
	cd frontend && npm run lint

check: lint test frontend-build ## CI-style validation

stop: ## Stop FinLab API and Vite listeners
	@pids=""; \
	for port in $(API_PORT) $(WEB_PORT); do \
		found="$$(lsof -ti tcp:"$$port" -sTCP:LISTEN 2>/dev/null)"; \
		pids="$$pids $$found"; \
	done; \
	pids="$$(echo $$pids | xargs -n1 2>/dev/null | sort -u | xargs 2>/dev/null)"; \
	if [ -n "$$pids" ]; then kill $$pids 2>/dev/null || true; echo "stopped"; else echo "nothing listening"; fi

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache dist build htmlcov .coverage frontend/dist frontend/node_modules/.cache

clean-data: ## Delete the local database and warehouse, keeping raw downloads
	rm -rf $(DATA_DIR)/warehouse $(DATA_DIR)/db $(DATA_DIR)/staging
