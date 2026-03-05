# breadforge Makefile — common development shortcuts

.PHONY: test test-unit test-integration lint fmt fmt-check check install clean

# ── Testing ──────────────────────────────────────────────────────────────────

test:
	uv run pytest

test-unit:
	uv run pytest tests/unit/

test-integration:
	uv run pytest tests/integration/

test-verbose:
	uv run pytest -v

test-cov:
	uv run pytest --cov=src/breadforge --cov-report=term-missing

# ── Linting / formatting ─────────────────────────────────────────────────────

lint:
	uv run ruff check src/ tests/

fmt:
	uv run ruff format src/ tests/

fmt-check:
	uv run ruff format --check src/ tests/

check: lint fmt-check test

# ── Dependencies ──────────────────────────────────────────────────────────────

install:
	uv sync --all-extras

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/ *.egg-info
