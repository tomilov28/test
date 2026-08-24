# workflow-benchmark

Benchmark harness comparing **Operaton** and **Flowable** as headless durable
BPMN runtimes behind a Python/FastAPI `WorkflowAdapter`.

## Purpose

Our system owns the domain: `Request`, `WorkItem`, `TaskResult`,
`WorkflowCommand` — state, results, audit.

The workflow engine owns BPMN execution state: gateways, parallel branches,
timers, wait states, jobs, retries, process versioning.

We never write business logic inside the engine. No Java delegates, no custom
plugins, no Spring beans, no direct engine-DB access. Integration happens
exclusively through each engine's public REST API and Python external workers.

## Ownership boundary

```
                    +---------------------------+
                    |   our system (owns domain) |
                    |   Request / WorkItem /     |
                    |   TaskResult / Command     |
                    +-------------+--------------+
                                  | public REST API
              +-------------------+--------------------+
              |  OPERATON                FLOWABLE      |
              |  (BPMN state, gateways, timers, jobs)  |
              +----------------------------------------+
```

## Structure

```
app/
  api/          FastAPI routes + pydantic schemas
  domain/       SQLAlchemy models + enums
  workflow/     adapter interface, operaton/flowable adapters,
                outbox dispatcher, reconciler, fault injection
  workers/      external Python workers (later phases)
bpmn/           BPMN files, one dir per engine
scripts/        wait_for_db, demo, benchmark_runner (later)
tests/          pytest suite (sqlite-backed unit tests)
docker/         app image + compose overlays
artifacts/      environment capture, benchmark results
```

## Quick start

```bash
make setup          # venv + deps + postgres up + migrations
make dev            # uvicorn on :8000
make test           # pytest (unit, no live DB required)
make up-operaton    # full Operaton stack: postgres + engine + app + worker daemon
make test-operaton  # integration suite + durability scenarios -> artifacts/operaton
make demo-operaton  # drive a request end-to-end, leave the stack running
make down           # stop all compose services + app/worker processes
```

Engine image versions are pinned in `versions.env` (never `:latest`).

## Operaton phase (pinned 2.1.4)

`make up-operaton` brings up, in order: shared Postgres, the separate Operaton
engine DB + engine container, migrations, deterministic fixture deployment
(`LONG_VISIT_POC` v1 and v2 as versions 1 and 2), the FastAPI harness (its
outbox dispatcher + reconciler run as background threads inside the app), and
the Python worker daemon (external task + human task completer).

Stack URLs after `make up-operaton` / `make demo-operaton`:

| Component          | URL                                        |
| ------------------ | ------------------------------------------ |
| Swagger UI         | http://localhost:8080/engine-rest/swaggerui/ |
| Operaton Cockpit   | http://localhost:8080/operaton/app/cockpit/ |
| Operaton Tasklist  | http://localhost:8080/operaton/app/tasklist/ |
| FastAPI docs       | http://localhost:8000/docs                 |
| Credentials        | `demo` / `demo`                            |

Canonical fixture: `bpmn/operaton/long_visit_poc.bpmn` (v1) and
`long_visit_poc_v2.bpmn` (v2) — one process key `LONG_VISIT_POC`. External
service task on topic `load_prisoner_data`, parallel human checks (v1:
`finance_check`, `relative_check`; v2 adds `discipline_check`), a `PT15S`
durable timer, and a `final_decision` user task. Version pinning is enforced
by the dispatcher (a request with `request_type_version` starts that exact
definition version).

The integration suite (`tests/test_long_visit_poc.py`, `-m integration`) covers
T01 deploy/versioning, T02 start, T03 external-worker real retry, T04/T05
parallel tasks + idempotent reconciliation, T06 branch completion, T08 parallel
join + timer, T10 v1/v2 versioning, T11 cancellation. Durability scenarios in
`scripts/operaton_scenarios.py` cover T07 (full stack restart with preserved DB)
and T09 (durable timer surviving an engine restart). Artifacts land in
`artifacts/operaton/` (`test-results.xml`, `benchmark.json`, `engine.log`,
`app.log`, `docker-stats.json`, `api-evidence/`).

## API surface (bootstrap phase)

| Method | Path                          | Purpose                                  |
| ------ | ----------------------------- | ---------------------------------------- |
| POST   | /requests                     | create request + enqueue START_PROCESS   |
| GET    | /requests                     | list requests (limit 100)                |
| GET    | /requests/{id}                | request detail + work items              |
| GET    | /requests/{id}/work-items     | list work items (with results)           |
| POST   | /work-items/{id}/complete     | save TaskResult + enqueue COMPLETE_TASK  |
| POST   | /requests/{id}/cancel         | enqueue CANCEL_PROCESS                   |
| POST   | /admin/reconcile              | run reconciler once                      |
| GET    | /health                       | liveness + db probe                      |

## Transactional outbox

Domain changes and engine commands commit atomically in one DB transaction:

```
BEGIN
  save TaskResult
  mark WorkItem COMPLETED
  create WorkflowCommand COMPLETE_TASK
COMMIT
```

A separate dispatcher thread claims PENDING commands and sends them to the
engine via the adapter. Commands survive restart: stale PROCESSING commands
are re-armed to PENDING.

## Reconciler

Engine active Human Tasks -> idempotent UPSERT of WorkItem. Runs manually
(`POST /admin/reconcile`) and periodically (default every 2s). Idempotent via
unique key `(request_id, task_definition_key, external_task_id)`.

## Environment note

This harness was bootstrapped in a memory-constrained cloud sandbox (~8 GiB
RAM, 2 vCPU, no swap, no `/dev/kvm`). Engines are run strictly sequentially
and engine quality must not be judged from OOM behaviour in this sandbox.
See `artifacts/environment.txt`.
