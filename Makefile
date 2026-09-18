.PHONY: db-up db-down backend-install migrate downgrade test lint api worker web reencrypt

db-up:
	docker compose up -d --wait db

db-down:
	docker compose down

backend-install:
	cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'

migrate:
	cd backend && .venv/bin/alembic upgrade head

# Destructive: refused unless ALLOW_DESTRUCTIVE_DOWNGRADE=1 and the target database is
# not in PROTECTED_DATABASE_NAMES (default "wip"). Scratch databases only.
downgrade:
	@test "$(ALLOW_DESTRUCTIVE_DOWNGRADE)" = "1" || { echo "refusing: set ALLOW_DESTRUCTIVE_DOWNGRADE=1 and point DATABASE_OWNER_URL at a scratch database (docs/OPERATIONS.md, Migrations)"; exit 1; }
	cd backend && ALLOW_DESTRUCTIVE_DOWNGRADE=1 .venv/bin/alembic downgrade base

test:
	cd backend && .venv/bin/pytest
	cd frontend && npm test

lint:
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .

api:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

worker:
	cd backend && .venv/bin/python -m app.worker

reencrypt:
	cd backend && .venv/bin/python scripts/reencrypt.py

web:
	cd frontend && npm run dev
