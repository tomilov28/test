SHELL := /bin/bash
PYTHON := .venv/bin/python
UV := uv
COMPOSE := docker compose
COMPOSE_OPERATON := docker compose -f docker-compose.yml -f docker-compose.operaton.yml
COMPOSE_FLOWABLE := docker compose -f docker-compose.yml -f docker-compose.flowable.yml
COMPOSE_ALL := docker compose -f docker-compose.yml -f docker-compose.operaton.yml -f docker-compose.flowable.yml

.PHONY: setup venv deps up-db migrate dev test up-operaton up-flowable down clean \
        test-operaton test-flowable benchmark demo-operaton demo-flowable report

# ---- bootstrap -----------------------------------------------------------

setup: venv deps up-db migrate
	@echo "== setup done =="

venv:
	test -d .venv || $(UV) venv --python 3.12 .venv

deps:
	$(UV) pip install --python .venv/bin/python -r requirements.txt

up-db:
	$(COMPOSE) up -d postgres
	$(PYTHON) scripts/wait_for_db.py

migrate:
	$(PYTHON) -m alembic upgrade head

dev:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000

test:
	$(PYTHON) -m pytest -q -m "not integration"

test-integration:
	$(PYTHON) -m pytest -q -m "integration"

# ---- engines -------------------------------------------------------------

up-operaton: create-enginedbs
	$(COMPOSE_OPERATON) up -d operaton
	$(PYTHON) scripts/wait_for_engine.py --engine OPERATON

up-flowable: create-enginedbs
	$(COMPOSE_FLOWABLE) up -d flowable
	$(PYTHON) scripts/wait_for_engine.py --engine FLOWABLE

create-enginedbs:
	$(PYTHON) scripts/create_databases.py

down:
	$(COMPOSE_ALL) down

clean: down
	rm -rf .venv __pycache__ .pytest_cache app/**/__pycache__
	rm -rf artifacts/benchmark artifacts/outbox artifacts/reconcile
	@echo "== clean done (artifacts/environment.txt preserved) =="

# ---- benchmark / demo (placeholders, wired in later phases) --------------

test-operaton:
	@echo "TODO(phase:operaton): run Operaton benchmark tests"
	$(PYTHON) -m pytest -q tests -k operaton || true

test-flowable:
	@echo "TODO(phase:flowable): run Flowable benchmark tests"
	$(PYTHON) -m pytest -q tests -k flowable || true

benchmark:
	@echo "TODO(phase:benchmark): run full comparative benchmark"
	$(PYTHON) -m scripts.benchmark_runner --help || true

demo-operaton:
	@echo "TODO(phase:operaton): run Operaton demo scenario"
	$(PYTHON) -m scripts.demo --engine OPERATON || true

demo-flowable:
	@echo "TODO(phase:flowable): run Flowable demo scenario"
	$(PYTHON) -m scripts.demo --engine FLOWABLE || true

report:
	@echo "TODO(phase:report): generate comparative report from artifacts/"
	@true
