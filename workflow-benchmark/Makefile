SHELL := /bin/bash
PYTHON := .venv/bin/python
UV := uv
COMPOSE := docker compose
COMPOSE_OPERATON := docker compose -f docker-compose.yml -f docker-compose.operaton.yml
COMPOSE_FLOWABLE := docker compose -f docker-compose.yml -f docker-compose.flowable.yml
COMPOSE_ALL := docker compose -f docker-compose.yml -f docker-compose.operaton.yml -f docker-compose.flowable.yml

.PHONY: setup venv deps up-db migrate dev test up-operaton up-flowable down clean \
        test-operaton test-flowable benchmark demo-operaton demo-flowable report \
        _operaton-infra

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

up-operaton: _operaton-infra
	$(PYTHON) -m scripts.stackctl start-worker
	@echo "== operaton stack up =="
	@echo "  Swagger UI        : http://localhost:8080/engine-rest/swaggerui/"
	@echo "  Operaton Cockpit  : http://localhost:8080/operaton/app/cockpit/"
	@echo "  Operaton Tasklist : http://localhost:8080/operaton/app/tasklist/"
	@echo "  API               : http://localhost:8000/docs"
	@echo "  Credentials       : demo / demo"

# postgres + operaton engine + migrated DB + fixtures + FastAPI harness
# (dispatcher + reconciler run inside the app). No Flowable.
_operaton-infra: create-enginedbs
	$(COMPOSE_OPERATON) up -d postgres operaton
	$(PYTHON) scripts/wait_for_engine.py --engine OPERATON
	$(PYTHON) -m alembic upgrade head
	$(PYTHON) -m scripts.deploy_fixtures --engine OPERATON --reset
	$(PYTHON) -m scripts.stackctl start-app

up-flowable: _flowable-infra
	$(PYTHON) -m scripts.stackctl start-worker --engine FLOWABLE
	@echo "== flowable stack up =="
	@echo "  Swagger UI        : http://localhost:8081/flowable-rest/docs"
	@echo "  API               : http://localhost:8000/docs"
	@echo "  Credentials       : rest-admin / test"

# postgres + flowable engine + migrated DB + fixtures + FastAPI harness
# (dispatcher + reconciler run inside the app). No Operaton.
_flowable-infra: create-enginedbs
	$(COMPOSE_FLOWABLE) up -d flowable
	$(PYTHON) scripts/wait_for_engine.py --engine FLOWABLE
	$(PYTHON) -m alembic upgrade head
	$(PYTHON) -m scripts.deploy_fixtures --engine FLOWABLE --reset
	$(PYTHON) -m scripts.stackctl start-app

create-enginedbs:
	$(PYTHON) scripts/create_databases.py

down:
	$(PYTHON) -m scripts.stackctl stop-app
	$(PYTHON) -m scripts.stackctl stop-worker --engine OPERATON
	$(PYTHON) -m scripts.stackctl stop-worker --engine FLOWABLE
	$(COMPOSE_ALL) down

clean: down
	rm -rf .venv __pycache__ .pytest_cache app/**/__pycache__
	rm -rf artifacts/benchmark artifacts/outbox artifacts/reconcile
	@echo "== clean done (artifacts/environment.txt preserved) =="

# ---- benchmark / demo ----------------------------------------------------

test-operaton: _operaton-infra
	@# tests manage their own external workers; stop a leftover daemon to avoid interference
	-$(PYTHON) -m scripts.stackctl stop-worker
	$(PYTHON) -m pytest -q -m integration \
		--junitxml=artifacts/operaton/test-results.xml
	$(PYTHON) -m scripts.operaton_scenarios --scenario all
	@echo "== test-operaton done =="

test-flowable: _flowable-infra
	@# tests manage their own external workers; stop a leftover daemon to avoid interference
	-$(PYTHON) -m scripts.stackctl stop-worker --engine FLOWABLE
	$(PYTHON) -m pytest -q -m integration \
		--junitxml=artifacts/flowable/test-results.xml \
		tests/test_flowable_integration.py tests/test_long_visit_poc_flowable.py
	$(PYTHON) -m scripts.flowable_scenarios --scenario all
	@# redeploy canonical fixtures so the artifact snapshot reflects benchmark-fixture state
	$(PYTHON) -m scripts.deploy_fixtures --engine FLOWABLE --reset
	$(PYTHON) -m scripts.collect_artifacts --engine FLOWABLE
	@echo "== test-flowable done =="

benchmark:
	@echo "TODO(phase:benchmark): run full comparative benchmark"
	$(PYTHON) -m scripts.benchmark_runner --help || true

demo-operaton: up-operaton
	$(PYTHON) -m scripts.demo --engine OPERATON
	@echo "== stack left running =="

demo-flowable: up-flowable
	$(PYTHON) -m scripts.demo --engine FLOWABLE
	@echo "== stack left running =="

report:
	@echo "TODO(phase:report): generate comparative report from artifacts/"
	@true
