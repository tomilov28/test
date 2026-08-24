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
make up-operaton    # bring up Operaton engine (later phases)
make up-flowable    # bring up Flowable engine (later phases)
make down           # stop all compose services
```

Engine image versions are pinned in `versions.env` (never `:latest`).

## API surface (bootstrap phase)

| Method | Path                          | Purpose                                  |
| ------ | ----------------------------- | ---------------------------------------- |
| POST   | /requests                     | create request + enqueue START_PROCESS   |
| GET    | /requests/{id}                | request detail + work items              |
| GET    | /requests/{id}/work-items     | list work items                          |
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
