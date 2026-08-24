# Progress

Status legend: `[ ]` not started, `[~]` in progress, `[x]` verified by actual run.

## Bootstrap

- [x] Environment captured -> `artifacts/environment.txt`
- [x] Versions pinned -> `versions.env` (Operaton 2.1.4, Flowable 8.0.0)
- [x] Project structure created
- [x] Domain model implemented (Request, WorkItem, TaskResult, WorkflowCommand)
- [x] WorkflowAdapter interface implemented (vendor-neutral)
- [x] FastAPI app starts
- [x] Database migrations work (alembic upgrade head)
- [x] Transactional outbox works (dispatcher + atomic claim)
- [x] Reconciler skeleton works (manual + periodic, idempotent)
- [x] `/health` responds
- [x] `make test` passes (15 tests: domain, dispatcher, reconciler, registry)
- [x] `make setup` runs end to end (venv/deps/postgres/migrate)

## Operaton (pinned 2.1.4)

- [x] `make up-operaton` starts full stack (postgres + engine + app + worker), no Flowable
- [x] Adapter REST mapping implemented (deploy/start/cancel/query/history/jobs/activity tree)
- [x] BPMN fixture deployed (`LONG_VISIT_POC` v1 + v2, `operaton:historyTimeToLive`)
- [x] External worker completes external service task (topic `load_prisoner_data`)
- [x] External worker real retry verified (T03: fail -> engine re-activates after retryTimeout)
- [x] Outbox START_PROCESS -> engine instance created (version-pinned dispatch)
- [x] Reconciler mirrors engine tasks to WorkItems (idempotent)
- [x] Request auto-closes on natural engine completion (CLOSED/COMPLETED)
- [x] Parallel tasks + join + `PT15S` durable timer -> `final_decision` (T08)
- [x] v1/v2 versioning verified (T10): old instance stays on v1, new starts on v2
- [x] Cancellation marks local WorkItems CANCELLED + engine instance ENDED (T11)
- [x] T07 full stack restart with preserved DB: state + reconcile survive, flow completes
- [x] T09 durable timer across engine restart: timer fires, reconciler surfaces `final_decision`
- [x] `make test-operaton` passes (12 integration + 16 unit), writes `test-results.xml`
- [x] `make demo-operaton` drives a request to COMPLETED and leaves the stack running
- [x] Memory floor determined (see below)
- [x] Artifacts collected -> `artifacts/operaton/` (benchmark.json, engine.log, docker-stats.json, api-evidence/)

### Operaton memory floor (headless REST runtime, Spring Boot distro)

| limit | JVM heap (70%) | outcome | RSS at idle |
|---|---|---|---|
| 2g (official doc) | ~1.4g | stable | ~860 MiB |
| 1g | ~700m | stable | ~378 MiB |
| 768m | ~537m | stable | ~357 MiB |
| 512m | ~358m | stable (headroom ~35%) | ~318 MiB |
| 384m | ~268m | works but ~90% used, no headroom | ~345 MiB |
| 256m | ~179m | JVM OOM-killed at startup | - |

**Recorded config**: `OPERATON_MEM_LIMIT=512m` + `MaxRAMPercentage=70.0` (practical floor with headroom; below this OOM). Config is env-parameterized in `docker-compose.operaton.yml`.

## Flowable (pinned 8.0.0)

- [x] `make up-flowable` starts engine container (separate DB, no Operaton)
- [x] Adapter REST mapping implemented (deploy/start/cancel/query/history/jobs/executions)
- [x] BPMN fixtures deployed (`bpmn/flowable/long_visit_v1.bpmn` / `long_visit_v2.bpmn`,
      `credit_decision`, `flowable:type="external"`)
- [x] External worker completes an external service task (topic `load_prisoner_data`,
      Flowable external-job protocol: `/external-job-api/acquire/jobs` + complete/fail)
- [x] External worker real retry verified (T03: fail -> engine re-activates ~30-35s)
- [x] Dead-letter path verified (`get_failed_jobs` -> `/management/deadletter-jobs`)
- [x] Outbox START_PROCESS -> engine instance created (version-pinned dispatch)
- [x] Reconciler mirrors engine tasks to WorkItems (idempotent)
- [x] Request auto-closes on natural engine completion (CLOSED/COMPLETED)
- [x] Parallel tasks + join + `PT15S` durable timer -> `final_decision` (T08)
- [x] v1/v2 versioning verified (T10): old instance stays on v1, new starts on v2
- [x] Cancellation marks local WorkItems CANCELLED + engine instance ENDED (T11)
- [x] F07/T07 full stack restart with preserved DB: state + reconcile survive, flow completes
- [x] F09/T09 durable timer across engine restart: timer fires, reconciler surfaces `final_decision`
- [x] Full T01..T11 flowable suite green: 13 integration tests (4 adapter + 9 LONG_VISIT_POC) + F07/F09
- [x] `make test-flowable` passes end to end (integration + scenarios + artifacts), writes `test-results.xml`
- [x] `make demo-flowable` drives a request to COMPLETED and leaves the stack running
- [x] Artifacts collected -> `artifacts/flowable/` (benchmark.json, engine.log, docker-stats.json, api-evidence/)
- [x] `BPMN_VENDOR_DIFF.md` written (namespaces, external declaration, deployment metadata, tooling)

### Flowable notes

* REST credentials: `rest-admin` / `test` (default bootstrap user).
* Flowable external worker uses a different protocol than Camunda/Operaton: a single
  acquire call locks jobs of ONE topic; failed jobs re-enter the queue after the async
  executor resets the expired lock (observed ~30-35s incl. executor cadence at PT6S).
* Cancellation can hit a transient PostgreSQL deadlock racing the async executor; the
  adapter retries 5xx on delete.
* History paginates at 10 by default; the adapter always requests `size=10000`.
* Deployment name: Flowable ignores the `deploymentName` form field and stores the
  uploaded file base name; `nameLike` filters need explicit SQL `%` wildcards.
* `flowable-rest` 8.0.0 ships no bundled admin UI and no external-topic listing
  endpoint (`/external-task/topic-names` is Operaton/Camunda-only); REST is the
  operational mechanism for Flowable.
* Engine image `flowable/flowable-rest:8.0.0` = 517 MB; boot ~15-18s; adapter 466 LOC
  (flowable.py 338 + flowable_worker.py 128).

## Comparative tests

- [x] Same BPMN fixture on both engines (identical behavior contract)
- [x] API surface parity across adapters
- [x] Outbox/dispatcher behavior under engine down/up
- [x] Reconciler idempotency under restart
- [x] Engine failure & retry semantics compared
- [x] `make benchmark` produces artifacts
- [~] `make report` writes comparative report

## Fault recovery & idempotency (phase 4)

Outbox commands were made **idempotent** via query-before-action instead of relying
on engine idempotency keys (neither engine guarantees a unique business key):

- [x] `START_PROCESS` -> `find_process_instance_by_business_key` before start; reuses the
      single existing instance on a retry after a lost response (T12)
- [x] `COMPLETE_TASK` -> `get_human_task` returns `None` on 404, i.e. "requested state
      already achieved", treated as success (T13)
- [x] `CANCEL_PROCESS` -> re-check actual engine state; already-ENDED instance is a
      no-op, so a lost cancel response retries idempotently (T14)
- [x] Fault injector (`app/workflow/fault_injector.py`): `loss` hides a *verified
      successful* engine response (real call runs first), `fail` rejects before the
      call to exhaust retries. Query methods are never faulted. Admin surface
      `POST /admin/faults/{arm,clear}` + `GET /admin/faults`.
- [x] T12: exactly one engine process instance per Request under repeated lost
      `start_process` responses (both engines, incl. racing edge cases)
- [x] T13: lost `complete_human_task` response -> retry sees task gone, business
      branch stays correct, flow reaches CLOSED/COMPLETED
- [x] T14: lost `cancel_process` response -> retry terminates idempotently,
      CLOSED/CANCELLED, instance ENDED, WorkItems CANCELLED
- [x] T15: exhausted outbox retries leave the Request ACTIVE with no business
      outcome; the technical failure is stored *engine-side*
- [x] T15 engine-side failure storage: Operaton records `failedExternalTask`
      INCIDENT (the job disappears from `/job` once retries=0); Flowable records a
      dead-letter job. `get_failed_jobs` surfaces both.
- [x] R1-R5 app restart scenarios converge on both engines
      (evidence under `artifacts/<engine>/faults/`)
- [x] Stress smoke: 50 instances/engine, no duplicate instances, no failed commands,
      no failed jobs; rough latency/throughput recorded (no winner)
- [x] Flowable cancellation deadlock: reproduced 25x, **recurrence=0/25** after fix,
      retries converge (see `cancel-deadlock-reproduction.json`); this race is
      Flowable-specific (async executor can re-activate a task after cancel), so the
      deadlock scenario runs for FLOWABLE only

### Bugs found & fixed in phase 4

1. **Reconciler raced a concurrent cancellation** (identity-map stale read): the
   auto-close code read the Request through SQLAlchemy's identity map after a
   `SELECT ... FOR UPDATE`; an already-loaded object is returned *without refreshing*,
   so the ACTIVE/outcome-None guard read stale values and overwrote a dispatcher's
   committed CLOSED/CANCELLED with CLOSED/COMPLETED (leaving WorkItems CANCELLED).
   Fixed with an **atomic conditional UPDATE** (`WHERE lifecycle_state=ACTIVE AND
   outcome IS NULL`) whose guard is re-evaluated against the latest committed row.
   Regression test `test_reconcile_does_not_overwrite_concurrent_cancel`.
2. **Operaton history endpoint filter mismatch**: `/history/process-instance`
   filters on `processInstanceBusinessKey` (the runtime endpoint uses `businessKey`);
   passing `businessKey` to history is silently ignored and returns ALL instances,
   which made the idempotent START reuse an unrelated instance. Fixed + verified the
   param names directly against the engine.
3. **Operaton external-task failure is an incident, not a job**: after
   `failure(retries=0)` the job is absent from `/job`; the engine's failure record is
   a `failedExternalTask` incident. `get_failed_jobs` now surfaces both jobs and
   incidents.

### Invariants (checked across recovered requests)

- one Request -> exactly one engine process instance (T12)
- one engine task -> at most one WorkItem (unique key `request_id +
  task_definition_key + external_task_id`)
- a CLOSED request is never reopened; completed/cancelled requests never return to
  ACTIVE
- an exhausted technical retry never sets a business outcome (request stays ACTIVE,
  outcome None)
- **cannot be fully guaranteed by the engine alone**: unique business keys
  (neither engine enforces uniqueness), and cancellation-vs-completion atomicity at
  the engine layer (Flowable async executor can briefly re-activate a task after
  cancel; the dispatcher's state re-check + reconcile-on-poll make the *local*
  outcome correct, but a cancelled engine task is not instant)

## Known limits (cloud sandbox)

- ~8 GiB RAM, 2 vCPU, no swap, no `/dev/kvm` -> engines run sequentially
- Engine quality must not be judged from OOM behavior in this sandbox
