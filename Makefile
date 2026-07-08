.PHONY: help install dev lint format test test-network clean
SCRIPT := swapsack

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the swapsack binary (auto-detects root/uv/pipx/pip)
	@if [ "$$(id -u)" = "0" ]; then \
		echo "Running as root, installing system-wide..."; \
		pip install .; \
	elif command -v uv >/dev/null 2>&1; then \
		echo "Installing with uv..."; \
		uv tool install .; \
	elif command -v pipx >/dev/null 2>&1; then \
		echo "Installing with pipx..."; \
		pipx install .; \
	else \
		echo "Tip: install uv or pipx for isolated installs (pacman -S uv, apt install pipx)"; \
		echo "Falling back to pip install --user ..."; \
		PIP_BREAK_SYSTEM_PACKAGES=1 pip install --user .; \
	fi
	@echo "Installed. Try: $(SCRIPT) --version"

dev:  ## Set up the development environment (deps + git hooks)
	uv sync
	uv run pre-commit install --install-hooks
	uv run pre-commit install --hook-type pre-push
	uv run pre-commit install --hook-type commit-msg

lint:  ## Run ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format:  ## Auto-fix lint and format
	uv run ruff check --fix .
	uv run ruff format .

test:  ## Run unit tests
	uv run pytest

test-network:  ## Run the live THORChain integration tests (read-only)
	uv run pytest -m network

clean:  ## Remove build/test artifacts
	rm -rf dist build *.egg-info src/*.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
