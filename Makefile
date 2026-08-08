# whatsapp-normalizer developer tasks.
# Run `make` or `make help` for the list.

PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
COMPOSE ?= docker compose
UVICORN_HOST ?= 127.0.0.1
UVICORN_PORT ?= 8000

.DEFAULT_GOAL := help

.PHONY: help install hooks dev worker test test-fast lint typecheck fmt up down restart ps logs clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install dev + runtime dependencies into the current environment
	$(PIP) install -r requirements-dev.txt

hooks: ## Install the git pre-commit hooks
	pre-commit install

dev: ## Run the API locally with auto-reload
	uvicorn app.main:app --reload --host $(UVICORN_HOST) --port $(UVICORN_PORT)

worker: ## Run the delivery worker locally
	$(PYTHON) -m app.worker

test: ## Run the test suite with coverage
	pytest --cov=app --cov-report=term-missing

test-fast: ## Run the test suite without coverage
	pytest

lint: ## Check lint rules, formatting and types (changes nothing)
	ruff check app tests
	black --check app tests
	mypy app

typecheck: ## Run mypy only
	mypy app

fmt: ## Auto-fix lint issues and format the code
	ruff check --fix app tests
	black app tests

up: ## Build and start the stack in the background
	$(COMPOSE) up -d --build

down: ## Stop the stack and remove its containers
	$(COMPOSE) down

restart: ## Restart the stack
	$(MAKE) down
	$(MAKE) up

ps: ## Show the status of the stack's services
	$(COMPOSE) ps

logs: ## Follow logs from all services
	$(COMPOSE) logs -f

clean: ## Remove caches and coverage artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
