.PHONY: run test test-unit lint format verify install-deps install-deps-dev update-deps images help

export PATH := $(HOME)/.local/bin:$(PATH)

default: help

# --- Dependencies ---

install-tools: ## Install uv if missing
	@command -v uv > /dev/null || { echo >&2 "uv is not installed. Installing..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	uv --version

install-deps: install-tools ## Install runtime dependencies
	uv sync

install-deps-dev: install-tools ## Install runtime + dev dependencies
	uv sync --group dev

update-deps: ## Update lock file and sync
	uv lock --upgrade && uv sync

# --- Run ---

run: ## Run the service locally
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

# --- Quality ---

format: ## Auto-format code with ruff
	uv run ruff format app/ tests/
	uv run ruff check --fix app/ tests/

lint: ## Run linters
	uv run ruff check app/ tests/
	uv run mypy app/

test: test-unit ## Run all tests

test-unit: ## Run unit tests
	uv run pytest tests/unit/ -v

verify: format lint test ## Format, lint, and test in one go

# --- Container ---

images: ## Build container image
	podman build -t ols-automator:latest -f Dockerfile .

# --- Help ---

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*##"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
