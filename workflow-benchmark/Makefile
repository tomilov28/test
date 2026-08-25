SHELL := /bin/bash
PYTHON := .venv/bin/python
UV := uv
COMPOSE := docker compose
COMPOSE_OPERATON := docker compose -f docker-compose.yml -f docker-compose.operaton.yml
COMPOSE_FLOWABLE := docker compose -f docker-compose.yml -f docker-compose.flowable.yml
COMPOSE_ALL := docker compose -f docker-compose.yml -f docker-compose.operaton.yml -f docker-compose.flowable.yml

OPERATON_TESTS := tests/test_operaton_integration.py tests/test_long_visit_poc.py \
	tests/test_fault_injection.py tests/test_outbox_lease.py
FLOWABLE_TESTS := tests/test_flowable_integration.py tests/test_long_visit_poc_flowable.py \
	tests/test_fault_injection.py tests/test_outbox_lease.py

.PHONY: setup venv deps up-db migrate dev test test-unit test-integration \
	up-operaton up-flowable down reset-state clean \
	test-operaton test-flowable fault-operaton fault-flowable \
	collect-operaton collect-flowable measure-resources \
	demo-operaton demo-flowable incident-operaton incident-flowable \
	benchmark report

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

test: test-unit

test-unit:
	$(PYTHON) -m pytest -q -m "not integration"

test-integration:
	$(PYTHON) -m pytest -q -m "integration"

# ---- engines -------------------------------------------------------------

up-operaton: _operaton-infra
	$(PYTHON) -m scripts.stackctl start-worker --engine OPERATON
	@echo "== operaton stack up =="
	@echo "  Swagger UI        : http://localhost:8080/engine-rest/swaggerui/"
	@echo "  Operaton Cockpit  : http://localhost:8080/operaton/app/cockpit/"
	@echo "  Operaton Tasklist : http://localhost:8080/operaton/app/tasklist/"
	@echo "  API               : http://localhost:8000/docs"
	@echo "  Credentials       : demo / demo"

# postgres + operaton engine + migrated DB + fixtures + FastAPI harness
# (dispatcher + reconciler run inside the app). No Flowable.
_operaton-infra:
	$(COMPOSE) up -d postgres
	$(PYTHON) scripts/wait_for_db.py
	$(PYTHON) scripts/create_databases.py
	$(COMPOSE_OPERATON) up -d operaton
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
_flowable-infra:
	$(COMPOSE) up -d postgres
	$(PYTHON) scripts/wait_for_db.py
	$(PYTHON) scripts/create_databases.py
	$(COMPOSE_FLOWABLE) up -d flowable
	$(PYTHON) scripts/wait_for_engine.py --engine FLOWABLE
	$(PYTHON) -m alembic upgrade head
	$(PYTHON) -m scripts.deploy_fixtures --engine FLOWABLE --reset
	$(PYTHON) -m scripts.stackctl start-app

create-enginedbs:
	$(PYTHON) scripts/create_databases.py

# ---- lifecycle (audit A09/A10) -------------------------------------------

down:
	$(PYTHON) -m scripts.stackctl stop-all
	COMPOSE_PROFILES=engine-operaton,engine-flowable $(COMPOSE_ALL) down

# Authoritative clean reset (audit A09): stop app/workers/engines, drop the
# PostgreSQL volume (wipes app + engine DBs), recreate + migrate + redeploy.
reset-state: down
	COMPOSE_PROFILES=engine-operaton,engine-flowable docker compose -f docker-compose.yml -f docker-compose.operaton.yml -f docker-compose.flowable.yml down -v
	$(COMPOSE) up -d postgres
	$(PYTHON) scripts/wait_for_db.py
	$(PYTHON) scripts/create_databases.py
	$(PYTHON) -m alembic upgrade head
	$(PYTHON) -m scripts.stackctl check-stale
	@echo "== reset-state done: app DB migrated, engine DBs empty =="

# ---- benchmark / test targets --------------------------------------------

# Audit A08: test-operaton runs ONLY Operaton integration tests; a down
# Operaton engine FAILS the run (never a silent skip). Same for test-flowable.
test-operaton: _operaton-infra
	$(PYTHON) -m scripts.stackctl stop-worker --engine OPERATON
	BENCH_ENGINE=OPERATON $(PYTHON) -m pytest -q -m integration \
		--junitxml=artifacts/operaton/test-results.xml $(OPERATON_TESTS)
	$(PYTHON) -m scripts.operaton_scenarios --scenario all
	$(PYTHON) -m scripts.collect_artifacts --engine OPERATON
	@echo "== test-operaton done =="

test-flowable: _flowable-infra
	$(PYTHON) -m scripts.stackctl stop-worker --engine FLOWABLE
	BENCH_ENGINE=FLOWABLE $(PYTHON) -m pytest -q -m integration \
		--junitxml=artifacts/flowable/test-results.xml $(FLOWABLE_TESTS)
	$(PYTHON) -m scripts.flowable_scenarios --scenario all
	$(PYTHON) -m scripts.deploy_fixtures --engine FLOWABLE --reset
	$(PYTHON) -m scripts.collect_artifacts --engine FLOWABLE
	@echo "== test-flowable done =="

# ---- fault recovery (phase 4/5) ------------------------------------------

fault-operaton: _operaton-infra
	$(PYTHON) -m scripts.stackctl stop-worker --engine OPERATON
	BENCH_ENGINE=OPERATON $(PYTHON) -m pytest -m integration \
		--junitxml=artifacts/operaton/fault-test-results.xml \
		$(OPERATON_TESTS) 2>&1 | tee artifacts/operaton/fault-suite.log
	# The integration suite drops its LONG_VISIT_POC fixture in module teardown;
	# redeploy before the scenarios so they can start processes (audit A07).
	$(PYTHON) -m scripts.deploy_fixtures --engine OPERATON --reset
	$(PYTHON) -m scripts.fault_scenarios --engine OPERATON 2>&1 | tee artifacts/operaton/fault-scenarios.log
	$(PYTHON) -m scripts.collect_artifacts --engine OPERATON
	@echo "== fault-operaton done =="

fault-flowable: _flowable-infra
	$(PYTHON) -m scripts.stackctl stop-worker --engine FLOWABLE
	BENCH_ENGINE=FLOWABLE $(PYTHON) -m pytest -m integration \
		--junitxml=artifacts/flowable/fault-test-results.xml \
		$(FLOWABLE_TESTS) 2>&1 | tee artifacts/flowable/fault-suite.log
	# The integration suite drops its LONG_VISIT_POC fixture in module teardown;
	# redeploy before the scenarios so they can start processes (audit A07).
	$(PYTHON) -m scripts.deploy_fixtures --engine FLOWABLE --reset
	$(PYTHON) -m scripts.fault_scenarios --engine FLOWABLE 2>&1 | tee artifacts/flowable/fault-scenarios.log
	$(PYTHON) -m scripts.deploy_fixtures --engine FLOWABLE --reset
	$(PYTHON) -m scripts.collect_artifacts --engine FLOWABLE
	@echo "== fault-flowable done =="

collect-operaton:
	$(PYTHON) -m scripts.collect_artifacts --engine OPERATON

collect-flowable:
	$(PYTHON) -m scripts.collect_artifacts --engine FLOWABLE

# Part B resource methodology: clean-DB measurement requires ONLY PostgreSQL +
# ONE engine. The FastAPI app and workers are stopped before each measurement so
# no harness process contributes to the engine container's memory/CPU numbers.
measure-resources: _operaton-infra
	$(PYTHON) -m scripts.stackctl stop-app
	$(PYTHON) -m scripts.stackctl stop-worker --engine OPERATON
	$(PYTHON) -m scripts.measure_resources --engine OPERATON
	$(COMPOSE_OPERATON) stop operaton
	$(COMPOSE_FLOWABLE) up -d flowable
	$(PYTHON) scripts/wait_for_engine.py --engine FLOWABLE
	$(PYTHON) -m scripts.deploy_fixtures --engine FLOWABLE --reset
	$(PYTHON) -m scripts.measure_resources --engine FLOWABLE
	@echo "== measure-resources done =="

# ---- demo / incident (Part C) --------------------------------------------
#
# ops_demo drives its own inline external worker (complete or terminal-fail),
# so the daemon worker started by up-<engine> is stopped first to avoid a race
# on the external task lock (deterministic demo/incident outcome).

demo-operaton: up-operaton
	$(PYTHON) -m scripts.stackctl stop-worker --engine OPERATON
	$(PYTHON) -m scripts.ops_demo --engine OPERATON --mode demo
	@echo "== stack left running =="

demo-flowable: up-flowable
	$(PYTHON) -m scripts.stackctl stop-worker --engine FLOWABLE
	$(PYTHON) -m scripts.ops_demo --engine FLOWABLE --mode demo
	@echo "== stack left running =="

incident-operaton: up-operaton
	$(PYTHON) -m scripts.stackctl stop-worker --engine OPERATON
	$(PYTHON) -m scripts.ops_demo --engine OPERATON --mode incident
	@echo "== stack left running =="

incident-flowable: up-flowable
	$(PYTHON) -m scripts.stackctl stop-worker --engine FLOWABLE
	$(PYTHON) -m scripts.ops_demo --engine FLOWABLE --mode incident
	@echo "== stack left running =="

# ---- Phase 6 authoritative benchmark / report -----------------------------

# Full authoritative benchmark: captures environment, runs unit tests, resets
# clean state, runs the complete Operaton and Flowable benchmark, collects the
# evidence into an immutable artifacts/runs/<UTC>-<SHA>/ directory, builds the
# report inputs and generates REPORT.md. Any mandatory stage failure aborts the
# run with a non-zero exit.
benchmark:
	$(PYTHON) -m scripts.run_benchmark

# (re)generate REPORT.md from the newest or a named run.
report:
	@test -n "$(RUN)" || test -d artifacts/runs || { echo "no benchmark runs found; run 'make benchmark' first"; exit 1; }
	$(PYTHON) -m scripts.generate_report --run $(if $(RUN),$(RUN),$(shell ls -1dt artifacts/runs/* | head -1))
