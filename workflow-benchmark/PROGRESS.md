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

- [ ] `make up-operaton` starts engine container
- [ ] Adapter REST mapping implemented (deploy/start/cancel/query)
- [ ] BPMN fixture deployed
- [ ] External worker completes a human task
- [ ] Outbox START_PROCESS -> engine instance created
- [ ] Reconciler mirrors engine tasks to WorkItems
- [ ] Fault/timer scenarios measured

## Flowable (pinned 8.0.0)

- [ ] `make up-flowable` starts engine container
- [ ] Adapter REST mapping implemented (deploy/start/cancel/query)
- [ ] BPMN fixture deployed
- [ ] External worker completes a human task
- [ ] Outbox START_PROCESS -> engine instance created
- [ ] Reconciler mirrors engine tasks to WorkItems
- [ ] Fault/timer scenarios measured

## Comparative tests

- [ ] Same BPMN fixture on both engines (identical behavior contract)
- [ ] API surface parity across adapters
- [ ] Outbox/dispatcher behavior under engine down/up
- [ ] Reconciler idempotency under restart
- [ ] Engine failure & retry semantics compared
- [ ] `make benchmark` produces artifacts
- [ ] `make report` writes comparative report

## Known limits (cloud sandbox)

- ~8 GiB RAM, 2 vCPU, no swap, no `/dev/kvm` -> engines run sequentially
- Engine quality must not be judged from OOM behavior in this sandbox
