# finlab
#
# `make app` launches the React frontend and FastAPI backend. `make run` launches
# the legacy Streamlit UI. `make bootstrap` takes a fresh clone all the way there.
# Python commands run through `uv`; frontend commands run through `npm`.
#
# Recipes use tabs, not spaces — that is Make, not a style choice.

SHELL := /bin/bash
.DEFAULT_GOAL := help

HOST ?= 127.0.0.1
PORT ?= 8501
AS_OF ?= $(shell date +%F)
DATA_DIR ?= data
WAREHOUSE := $(DATA_DIR)/warehouse/fact

.PHONY: help setup env ingest concepts metrics run ui api web stop test lint fmt check \
        coverage status clean clean-data bootstrap universe submissions schedule \
        daemon queue schedule-daemon prices-yahoo

help: ## Show the available targets
	@echo "finlab — make targets"
	@echo
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## /  {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  Variables: HOST=$(HOST) PORT=$(PORT) AS_OF=$(AS_OF)"

# --- setup ------------------------------------------------------------------

setup: ## Install dependencies and create the database
	uv sync --all-extras
	uv run finlab db init

env: ## Create .env from the example if it is missing
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "created .env — set FINLAB_SEC_USER_AGENT to a real contact address"; \
		echo "the SEC blocks anonymous traffic, so ingestion will refuse to run until you do"; \
	else \
		echo ".env already exists"; \
	fi

# --- data -------------------------------------------------------------------

ingest: ## Fetch companyfacts for the PoC universe (~50 SEC requests)
	uv run finlab ingest poc

START ?= 1996-01-01
END ?= $(shell date +%F)

prices-yahoo: ## Backfill daily raw OHLCV through Yahoo (override START/END)
	uv run finlab ingest prices-yahoo --start $(START) --end $(END)

universe: ## Capture today's universe snapshot — run this DAILY, it cannot be backfilled
	uv run finlab ingest universe

submissions: ## Backfill filing acceptance times and entity descriptors
	uv run finlab ingest submissions

schedule: ## Install the systemd timer for the daily universe snapshot
	mkdir -p $(HOME)/.config/systemd/user
	cp deploy/finlab-universe.service deploy/finlab-universe.timer $(HOME)/.config/systemd/user/
	systemctl --user daemon-reload
	systemctl --user enable --now finlab-universe.timer
	@systemctl --user list-timers finlab-universe.timer --no-pager
	@echo
	@echo "run 'loginctl enable-linger $(USER)' so it fires when you are logged out"

concepts: ## Load the concept map and rebuild derived Q4
	uv run finlab concepts load

metrics: ## Compute the ratio set at AS_OF
	uv run finlab metrics --as-of $(AS_OF)

bootstrap: setup env ingest submissions concepts metrics universe ## Fresh clone to a running app, in one go
	@echo
	@echo "ready — now run: make app (React + FastAPI), or make run (legacy Streamlit)"

# --- the daemon -------------------------------------------------------------

daemon: ## Run finlabd in the foreground (holds the single write connection)
	uv run finlabd

queue: ## Show the job queue by state
	@uv run python -c "from finlab.config import get_settings; from finlab.db.conn import write_conn; \
from finlab.daemon import queue; \
print(queue.counts(write_conn(get_settings()).__enter__()))"

schedule-daemon: ## Install and start finlabd as a systemd user service
	mkdir -p $(HOME)/.config/systemd/user
	cp deploy/finlabd.service $(HOME)/.config/systemd/user/
	systemctl --user daemon-reload
	systemctl --user enable --now finlabd.service
	@systemctl --user status finlabd.service --no-pager | head -5

# --- the app ----------------------------------------------------------------

run: ## Launch the read-only UI on HOST:PORT
	uv run finlab ui --host $(HOST) --port $(PORT)

ui: run ## Alias for run

api: ## Launch the FastAPI backend on HOST:8000
	uv run finlab api --host $(HOST) --port 8000

web: ## Launch the React/Vite frontend on HOST:5173
	cd frontend && npm run dev -- --host $(HOST) --port 5173

app: ## Launch FastAPI and Vite together
	@uv run finlab api --host $(HOST) --port 8000 & \
	api_pid=$$!; \
	trap 'kill $$api_pid 2>/dev/null || true' EXIT INT TERM; \
	cd frontend && npm run dev -- --host $(HOST) --port 5173

# --- checks -----------------------------------------------------------------

test: ## Run the test suite
	uv run pytest

lint: ## Lint and check formatting without changing anything
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Format and autofix
	uv run ruff format .
	uv run ruff check --fix .

coverage: ## Concept coverage report; exits non-zero below 95%
	uv run finlab concepts coverage --as-of $(AS_OF)

status: ## Show schema version, table names and row counts
	uv run finlab db status

check: lint test ## Everything CI would run

# --- cleaning ---------------------------------------------------------------

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache dist build htmlcov .coverage
	find src tests -name '__pycache__' -type d -prune -exec rm -rf {} +

clean-data: ## Delete the warehouse and database, but keep raw downloads
	rm -rf $(DATA_DIR)/warehouse $(DATA_DIR)/db $(DATA_DIR)/staging
	@echo "raw downloads kept in $(DATA_DIR)/raw — 'make concepts metrics' after re-ingest"

stop: ## Stop FastAPI, Vite and the legacy Streamlit app
	@pids=""; \
	for port in 8000 5173 $(PORT); do \
		found="$$(lsof -ti tcp:"$$port" -sTCP:LISTEN 2>/dev/null)"; \
		pids="$$pids $$found"; \
	done; \
	pids="$$(echo $$pids | xargs -n1 2>/dev/null | sort -u | xargs 2>/dev/null)"; \
	if [ -n "$$pids" ]; then \
		kill $$pids 2>/dev/null || true; \
		echo "stopped finlab app processes"; \
	else \
		echo "no finlab app processes listening on ports 8000, 5173 or $(PORT)"; \
	fi
