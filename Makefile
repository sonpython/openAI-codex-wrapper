.PHONY: dev up down build migrate lint format test test-compat test-compat-collect verify-codex deploy backup restore runbook help

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
	@echo "  test-compat    Full SDK compat suite via docker-compose.test.yml"
	@echo "  test-compat-collect  Collect compat tests without running (no Docker needed)"
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

test-compat:
	docker compose -f docker-compose.test.yml up -d --build --wait
	docker compose -f docker-compose.test.yml exec -T \
		-e COMPAT_EXTERNAL_STACK=1 \
		test-runner \
		uv run alembic upgrade head
	docker compose -f docker-compose.test.yml exec -T \
		-e COMPAT_EXTERNAL_STACK=1 \
		test-runner \
		uv run pytest tests/compat/test-python-sdk.py -v \
		  --cov=src/gateway --cov=src/codex --cov=src/workers \
		  --cov-report=term-missing --cov-fail-under=75
	docker compose -f docker-compose.test.yml exec -T \
		test-runner \
		sh -c "cd tests/compat/test_node_sdk && pnpm test"
	docker compose -f docker-compose.test.yml down -v

test-compat-collect:
	uv run pytest --collect-only tests/compat -q 2>&1 | head -40

# ── Codex pre-flight (C1 + C11) ───────────────────────────────────────────────
# Runs verify-codex.sh INSIDE the gateway container.
# Must be executed AFTER `make up` and BEFORE phase-02 implementation.
# Non-zero exit blocks CI and signals a version/flag mismatch with 0.125.0.
verify-codex:
	docker compose exec gateway bash /app/scripts/verify-codex.sh

# ── Production deploy ─────────────────────────────────────────────────────────
deploy:
	docker compose -f docker-compose.yml -f docker-compose.production.yml pull
	docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --remove-orphans

# ── Backup / restore ──────────────────────────────────────────────────────────
backup:
	docker compose -f docker-compose.yml -f docker-compose.production.yml \
		exec postgres-backup /usr/local/bin/postgres-backup.sh

restore:
	@test -n "$(KEY)" || (echo "Usage: make restore KEY=postgres/codex_wrapper-<timestamp>.dump.age" && exit 1)
	docker compose -f docker-compose.yml -f docker-compose.production.yml \
		run --rm postgres-backup \
		/usr/local/bin/postgres-restore.sh "$(KEY)"

# ── Runbook ───────────────────────────────────────────────────────────────────
runbook:
	@command -v open >/dev/null 2>&1 && open docs/operations-runbook.md || \
	command -v xdg-open >/dev/null 2>&1 && xdg-open docs/operations-runbook.md || \
	cat docs/operations-runbook.md
