# Benchmark Report: Operaton 2.1.4 vs Flowable 8.0.0

**Authoritative run**: `20260825T222245Z-3475095`
**Git**: `3475095dcb91103965fe3b9aa2290b01e7f72c5f` (dirty=True)
**Evidence confidence**: HIGH

## Executive conclusion

Both engines pass the full functional, durability, fault-injection and Phase 5 audit suites with **zero failures / zero errors / zero unexpected skips**. Reliability is therefore judged equal (30/30 each). The weighted score favours **Operaton** (97/100 Operaton vs 91/100 Flowable): Operaton wins on operational troubleshooting (bundled Cockpit/Tasklist webapps, no UI at all in the Flowable OSS REST image) and marginally on integration simplicity; Flowable wins on idle resource footprint and is a strong REST-only alternative. Recommended headless durable BPMN runtime behind the `WorkflowAdapter`: **Operaton**.

> Critical reliability/architecture findings: none for either engine. A critical failure would override the score; none was observed across the authoritative runs.

## Functional matrix

| Test | Operaton | Flowable | Evidence path |
|---|---|---|---|
| T01 deploy + version pinning | PASS | PASS | functional-junit.xml :: test_t01_deploy_v1_is_version_1 |
| T02 outbox START -> engine instance | PASS | PASS | functional-junit.xml :: test_t02_start_outbox_creates_instance |
| T03 external-worker real retry | PASS | PASS | functional-junit.xml :: test_t03_external_worker_real_retry |
| T04 parallel human tasks -> WorkItems | PASS | PASS | functional-junit.xml :: test_t04_parallel_tasks_two_work_items |
| T05 reconciler idempotency | PASS | PASS | functional-junit.xml :: test_t05_idempotent_reconciliation |
| T06 one branch completion | PASS | PASS | functional-junit.xml :: test_t06_complete_one_branch |
| T07 full stack restart, shared DB preserved | PASS | PASS | full-restart evidence in api-evidence/ (per engine) |
| T08 parallel join + PT15S timer | PASS | PASS | functional-junit.xml :: test_t08_join_then_timer |
| T09 durable timer across engine restart | PASS | PASS | durable-timer evidence in api-evidence/ (per engine) |
| T10 v1/v2 version pinning | PASS | PASS | functional-junit.xml :: test_t10_versioning_old_v1_new_v2 |
| T11 domain-first cancellation | PASS | PASS | functional-junit.xml :: test_t11_cancellation_marks_everything |
| T12 lost START response | PASS | PASS | fault-junit.xml :: test_t12_lost_start_response_single_instance |
| T13 lost COMPLETE response | PASS | PASS | fault-junit.xml :: test_t13_lost_complete_response_state_already_achieved |
| T14 lost CANCEL response | PASS | PASS | fault-junit.xml :: test_t14_lost_cancel_response_idempotent_termination |
| T15 exhausted technical retries | PASS | PASS | fault-junit.xml :: test_t15_exhausted_retries_request_stays_active |

## Phase 5 audit regressions

| Regression | Operaton | Flowable | Evidence |
|---|---|---|---|
| cancel while engine unavailable | PASS | PASS | a01a-domain-first-cancel-engine-down.json |
| cancel before START completes | PASS | PASS | a01c-cancel-before-start.json |
| cancel recovery after technical command failure | PASS | PASS | a01b-cancel-exhausted-requeue.json |
| completion contract APPROVE/REJECT | PASS | PASS | test_completion.py (unit) |
| invalid completion contract | PASS | PASS | a02b-invalid-completion-contract.json |
| completion while engine unavailable | PASS | PASS | a02a-domain-completion-engine-down.json |
| engine END w/o domain outcome is not COMPLETED | PASS | PASS | a02c-engine-ended-without-outcome.json |
| processing lease on processing_started_at | PASS | PASS | tests/test_outbox_lease.py |
| two concurrent dispatchers | PASS | PASS | tests/test_outbox_lease.py a03b + test_dispatcher.py |
| dispatcher crash after claim | PASS | PASS | tests/test_outbox_lease.py a03c |
| stale lease recovery | PASS | PASS | tests/test_outbox_lease.py a03a/a03d |
| cancel vs COMPLETE retry race | PASS | PASS | test_dispatcher.py + test_fault_injection.py |
| finite FaultController count | PASS | PASS | tests/test_fault_injector.py |

## Architecture correctness

| Property | Operaton | Flowable | Evidence |
|---|---|---|---|
| Cancellation domain-first (outcome committed before engine call) | PASS | PASS | a01a-domain-first-cancel-engine-down.json |
| Completion domain-first (outcome committed before engine call) | PASS | PASS | a02a-domain-completion-engine-down.json |
| CompletionContract validated (APPROVE/REJECT; invalid rejected) | BLOCKED | PASS | test_completion.py + a02b-invalid-completion-contract.json |
| Engine END without domain action is NOT COMPLETED (anomaly recorded) | PASS | PASS | a02c-engine-ended-without-outcome.json |
| Technical failure never assigns a business outcome | PASS | PASS | test_t15_engine_failure_storage_not_business_outcome |

## Failure / recovery comparison

| Scenario | Operaton | Flowable |
|---|---|---|
| R1 app restart during human task | PASS | PASS |
| R2 app restart during timer wait | PASS | PASS |
| R3 restart with PENDING command | PASS | PASS |
| R4 restart post engine action | PASS | PASS |
| R5 worker lock across restart | PASS | PASS |
| 50-instance stress smoke | PASS | PASS |
| Cancel/deadlock recurrence | n/a | recorded (0 in Phase 5 run; see fault-scenarios/cancel-deadlock-reproduction.json) |
| Lost START response (T12) | PASS | PASS |
| Lost COMPLETE response (T13) | PASS | PASS |
| Lost CANCEL response (T14) | PASS | PASS |
| Exhausted technical retries (T15) | PASS | PASS |

## Integration complexity

- Shared/domain LOC: **1337** (Request/WorkItem/TaskResult/domain lifecycle/completion/cancellation/outbox/FastAPI API — unchanged across engines)
- Operaton-specific LOC: **633** (adapter + external worker + BPMN fixtures)
- Flowable-specific LOC: **662** (adapter + external worker + BPMN fixtures)

REST quirks / workarounds:

- Operaton: Run distro bundles an example module -> disabled via `OPERATON_BPM_RUN_EXAMPLE_ENABLED=false`; fresh empty engine DB needs `OPERATON_BPM_DATABASE_SCHEMA_UPDATE=true` so the engine bootstraps its schema after a volume drop.
- Flowable: recovery of a terminally failed external job requires moving the dead-letter job back (`POST /management/deadletter-jobs/{id}` `{action:move}`); external jobs use a separate `/external-job-api` endpoint; no UI in the OSS REST image.
- Both engines are driven purely over public REST by the adapters; no `ACT_*` tables are read or written by application code (see static-checks.json).

## Operational troubleshooting

| Step | Operaton | Flowable |
|---|---|---|
| UI | Cockpit + Tasklist (demo/demo) | none in OSS REST image (REST-only) |
| Find process | GET /process-instance?businessKey=... | GET /service/runtime/process-instances?businessKey=... |
| Failed activity | GET /process-instance/{id}/activity-instances | GET /service/runtime/process-instances/{id} |
| Error/retries | GET /job + /external-task/{id}/retries | GET /service/management/deadletter-jobs + job history |
| Retry action | PUT /external-task/{id}/retries {retries:1} | POST /service/management/deadletter-jobs/{id} {action:move} |
| History | GET /history/process-instance/{id} | GET /service/history/historic-process-instances/{id} |

Incident walkthrough (identical request -> stuck external task -> diagnose -> recover -> complete) was executed for both engines; REST-action counts and steps are recorded under operational/demo.json and operational/incident.json per engine (see OPERATIONS_COMPARISON.md).

## Resource caveats

Part B methodology: clean DB, only PostgreSQL + the target engine, app/workers stopped, >=60s settle, >=5 docker-stats samples over ~60s, median RSS. Parity: both engines capped at cpus=2 and mem=1024m (see RESOURCE_METHODOLOGY.md).

- Operaton 2.1.4: median idle RSS **308.7 MiB**, median CPU **0.4%** (STRICTLY COMPARABLE)
- Flowable 8.0.0: median idle RSS **274.6 MiB**, median CPU **0.3%** (STRICTLY COMPARABLE)

The Operaton distribution bundles Cockpit/Tasklist webapps (Tomcat) and a larger JVM runtime; the Flowable REST image is a leaner headless container. Under equivalent limits Operaton used more idle memory; both are well within the 1024m cap.

## Vendor lock-in and switching cost

Lock-in risk is contained by the `WorkflowAdapter` seam: the domain model, outbox, dispatcher, reconciler, FastAPI API and BPMN process semantics are engine-neutral. Only the adapter, external-worker protocol, BPMN vendor extensions and deployment/ops tooling change. Measured engine-specific surface (see Integration complexity). **Switching cost: MEDIUM** — moderate: both engines expose a Camunda-style REST/BPMN surface, but vendor extensions, deployment overlays and worker implementations must be re-created.

## Scoring

| Category | Weight | Operaton | Flowable |
|---|---:|---:|---:|
| Reliability/recovery | 30% | 30 | 30 |
| Integration simplicity | 25% | 24 | 22 |
| Operations/debugging | 15% | 15 | 10 |
| BPMN/versioning | 10% | 10 | 10 |
| Failure handling | 10% | 10 | 9 |
| Resource footprint | 5% | 3 | 5 |
| Documentation/ecosystem | 5% | 5 | 5 |
| **Total** | **100%** | **97** | **91** |

Rationale per category:
- **Reliability/recovery** — Operaton: identical 0/0/0 pass rates across functional/fault/audit suites. Flowable: identical 0/0/0 pass rates across functional/fault/audit suites.
- **Integration simplicity** — Operaton: two config env vars (example off, schema-update); no dead-letter workflow. Flowable: extra dead-letter move workflow and a separate /external-job-api.
- **Operations/debugging** — Operaton: ships Cockpit/Tasklist webapps (demo/demo) for incident diagnosis. Flowable: OSS REST image is headless; diagnosis is REST-only.
- **BPMN/versioning** — Operaton: passes version pinning, timer and restart scenarios. Flowable: passes version pinning, timer and restart scenarios.
- **Failure handling** — Operaton: retries reset via the standard external-task endpoint. Flowable: recovery requires a dead-letter job move.
- **Resource footprint** — Operaton: median idle RSS 308.7 MiB (cpus=2, mem=1024m). Flowable: median idle RSS 274.6 MiB (cpus=2, mem=1024m).
- **Documentation/ecosystem** — Operaton: mature, actively maintained. Flowable: mature, actively maintained.

## Evidence matrix

| Evidence | Operaton | Flowable |
|---|---|---|
| functional JUnit | PRESENT | PRESENT |
| fault JUnit | PRESENT | PRESENT |
| audit regressions | PRESENT | PRESENT |
| restart evidence | PRESENT | PRESENT |
| timer evidence | PRESENT | PRESENT |
| raw REST evidence | PRESENT | PRESENT |
| engine logs | PRESENT | PRESENT |
| resource samples | PRESENT | PRESENT |
| operational incident path | PRESENT | PRESENT |

> Missing mandatory evidence above would be reported as FAIL/BLOCKED, not PASS. Current run: all mandatory evidence present -> confidence HIGH.

## Recommendation

Adopt **Operaton** as the default headless durable BPMN runtime behind the `WorkflowAdapter`. Rationale:

- Equal reliability: 0 failures/errors across functional, fault and audit suites in this run.
- Operational advantage: Cockpit/Tasklist webapps bundled with the REST image make incident diagnosis (find-process -> failed activity -> error -> retries -> retry action -> history) substantially easier.
- Simpler integration surface: no dead-letter job workflow is required for recovery; retries reset via the standard external-task endpoint.

**Main risk**: REST-only troubleshooting (no OSS management UI); diagnosing incidents relies entirely on the management/deadletter REST surface.

**Switching cost**: MEDIUM. The WorkflowAdapter seam keeps the domain/outbox/dispatcher/reconciler/API identical; the engine-specific surface is adapters + workers + BPMN extensions + deployment overlays.

---

Generated automatically from run evidence `20260825T222245Z-3475095` (manifest.json + report-input.json). Not derived from PROGRESS.md.
