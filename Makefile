.PHONY: install install-hooks run test test-unit test-integration lint format format-check typecheck check \
	migrate migrate-docker seed daily-batch \
	docker-up docker-down docker-reset docker-logs docker-migrate docker-worker docker-cmir-consumer

install:
	uv sync
	$(MAKE) install-hooks

install-hooks:
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit

run:
	uv run uvicorn app.main:app --reload

test:
	uv run pytest tests/ -v

test-unit:
	uv run pytest tests/unit -v

test-integration:
	uv run pytest tests/integration -v

lint:
	uv run ruff check app/ scripts/ tests/ alembic/

format:
	uv run ruff format app/ scripts/ tests/ alembic/

format-check:
	uv run ruff format --check app/ scripts/ tests/ alembic/

typecheck:
	uv run mypy app/

check: lint format-check typecheck test

# Local Postgres reachable directly (DATABASE_URL from the environment/.env).
migrate:
	uv run alembic upgrade head

# Same migration, run inside the app image against the Compose Postgres --
# use this instead of `migrate` when nothing outside Docker can reach the DB.
migrate-docker:
	docker compose --profile tools run --rm migrate

seed:
	uv run python scripts/demo/seed_master_data.py

# One manual batch enqueue+drain, the same entrypoint the nightly
# Container Apps Job runs on cron (see docs/DEPLOYMENT.md).
daily-batch:
	uv run python scripts/ops/run_daily_batch.py

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

# Also drops the Postgres volume -- for when a schema change needs a
# genuinely fresh database, not just a container restart.
docker-reset:
	docker compose down -v

docker-logs:
	docker compose logs -f

docker-migrate:
	docker compose --profile tools run --rm migrate

# One batch drain inside Docker (docker-compose.yml's stand-in for the
# Azure Container Apps Job).
docker-worker:
	docker compose --profile tools run --rm fines-projection-worker

# Local stand-in for the standalone Service Bus consumer process.
docker-cmir-consumer:
	docker compose --profile tools run --rm cmir-consumer
