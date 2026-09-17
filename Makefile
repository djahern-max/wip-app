.PHONY: db-up db-down backend-install migrate downgrade test lint api web

db-up:
	docker compose up -d --wait db

db-down:
	docker compose down

backend-install:
	cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'

migrate:
	cd backend && .venv/bin/alembic upgrade head

downgrade:
	cd backend && .venv/bin/alembic downgrade base

test:
	cd backend && .venv/bin/pytest

lint:
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .

api:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

web:
	cd frontend && npm run dev
