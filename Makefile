.PHONY: dev up down build migrate lint format test verify-codex help

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo "codex-wrapper development targets"
	@echo ""
	@echo "  dev            Run gateway locally with uvicorn hot-reload"
	@echo "  up             docker compose up --build (all services)"
	@echo "  down           docker compose down"
	@echo "  build          docker compose build (no cache)"
	@echo "  migrate        Run alembic upgrade head against DATABASE_URL"
	@echo "  lint           ruff check + ruff format --check + mypy"
	@echo "  format         ruff format (auto-fix)"
	@echo "  test           pytest -q tests/unit"
	@echo "  verify-codex   Assert codex 0.125.0 + required flags inside gateway container"

# ── Local development ─────────────────────────────────────────────────────────
dev:
	uv run uvicorn src.gateway.app:create_app \
		--factory --host 0.0.0.0 --port 8000 --reload --log-level info

# ── Docker ────────────────────────────────────────────────────────────────────
up:
	docker compose up --build

down:
	docker compose down

build:
	docker compose build --no-cache

# ── Database migrations ───────────────────────────────────────────────────────
migrate:
	uv run alembic upgrade head

# ── Code quality ──────────────────────────────────────────────────────────────
lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy src

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	uv run pytest -q tests/unit

# ── Codex pre-flight (C1 + C11) ───────────────────────────────────────────────
# Runs verify-codex.sh INSIDE the gateway container.
# Must be executed AFTER `make up` and BEFORE phase-02 implementation.
# Non-zero exit blocks CI and signals a version/flag mismatch with 0.125.0.
verify-codex:
	docker compose exec gateway bash /app/scripts/verify-codex.sh
