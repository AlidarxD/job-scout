.PHONY: install lint fmt typecheck test test-int up down migrate ingest serve eval

PY ?= .venv/bin/python

install:
	uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[dev,llm]'

lint:
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

fmt:
	$(PY) -m ruff format src tests
	$(PY) -m ruff check --fix src tests

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest -q -m "not integration"

test-int:
	$(PY) -m pytest -q -m integration

up:
	docker compose up -d --build api

down:
	docker compose down

migrate:
	$(PY) -m alembic upgrade head

ingest:
	$(PY) -m jobscout.cli ingest --all

serve:
	$(PY) -m uvicorn jobscout.api.app:create_app --factory --reload

eval:
	$(PY) -m jobscout.cli eval extraction --extractor rules
