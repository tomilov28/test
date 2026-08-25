# Operations Comparison (Part C)

This document compares the two engines as *headless BPMN runtimes operated by
our Python harness*, focused on the operational surfaces that matter in the
benchmark: happy-path walking of a request, and detecting + recovering from a
terminal external-worker failure. It deliberately avoids a "winner" - that is
Phase 6's job. The mechanics and the REST surfaces differ; the benchmark's
logical contract is identical for both.

Implementation: `scripts/ops_demo.py` (`--mode demo` / `--mode incident`).
Evidence: `artifacts/<engine>/faults/ops_demo_{demo,incident}.json`.
Driven by `make demo-<engine>` / `make incident-<engine>`.

## The workload

`LONG_VISIT_POC` v1: external service task on topic `load_prisoner_data`,
parallel human checks `finance_check` + `relative_check`, parallel join,
`PT15S` durable timer, final `final_decision` user task, outcome `APPROVE`.

## Happy-path demo

`make demo-<engine>` creates one request through the benchmark API, waits for
the instance to start, runs the external worker once, completes both parallel
human tasks, waits out the `PT15S` timer, approves `final_decision`, and waits
for the request to reach `CLOSED/COMPLETED`. It prints the request id, the
engine instance id, the management URLs and credentials.

| Engine | Result | Outcome |
| ------ | ------ | ------- |
| Operaton 2.1.4 | PASS (see `artifacts/operaton/faults/ops_demo_demo.json`) | `COMPLETED` |
| Flowable 8.0.0 | PASS (see `artifacts/flowable/faults/ops_demo_demo.json`) | `COMPLETED` |

Management surfaces used by an operator after the demo:

| Engine | URL | Credentials |
| ------ | --- | ----------- |
| Operaton | Cockpit http://localhost:8080/operaton/app/cockpit/ ; Tasklist http://localhost:8080/operaton/app/tasklist/ ; REST http://localhost:8080/engine-rest | `demo` / `demo` |
| Flowable | REST http://localhost:8081/flowable-rest/service ; external-job API http://localhost:8081/flowable-rest/external-job-api ; IDM http://localhost:8081/flowable-idm | `rest-admin` / `test` |

## Terminal external-worker failure incident

`make incident-<engine>` reproduces the operational worst case for a durable
workflow: the external worker fails the `load_prisoner_data` task with
`retries=0`, so the process is stuck on that activity. The script then
diagnoses the incident **purely through each engine's public REST API** (it
counts every REST call), performs the operator's recovery action, and walks the
process to a completed outcome.

### Operaton

- Diagnosis (5 REST actions): `GET /process-instance/{id}`, `GET
  /process-instance/{id}/activity-instances`, `GET /job`, `GET /incident`,
  `GET /history/activity-instance`.
- Failure observed: `retries: 0`, exception `incident failedExternalTask on
  load_prisoner_data`; process stuck on `load_prisoner_data`.
- Recovery action: `PUT /external-task/{task_id}/retries {"retries": 1}`,
  then the external worker completes the task.
- UI path (Cockpit): Process Instances -> search by business key -> Incidents
  tab -> Retry (or set retries via REST).
- Evidence: `artifacts/operaton/faults/ops_demo_incident.json`.

### Flowable (OSS)

- The OSS distribution has no Cockpit-equivalent management UI (that is an EE
  feature), so diagnosis and recovery are REST-only.
- Diagnosis (4 REST actions): `GET /service/runtime/process-instances/{id}`,
  `GET /service/runtime/executions`, `GET
  /service/management/deadletter-jobs`, `GET
  /service/history/historic-activity-instances`.
- Failure observed: `retries: 0`, exception `external service down (simulated
  terminal failure)`; the process is stuck on `load_prisoner_data` and the job
  sits in the dead-letter queue.
- Recovery action: `POST /service/management/deadletter-jobs/{job_id}
  {"action": "move"}` moves the job back from the dead-letter queue to the
  normal queue, then the external worker completes it.
- Evidence: `artifacts/flowable/faults/ops_demo_incident.json`.

## Mechanics that differ (and why the contract still holds)

| Aspect | Operaton | Flowable |
| ------ | -------- | -------- |
| External task protocol | `fetch_and_lock` on `/external-task`; complete/fail via `/external-task/{id}/{complete,failure}` | External jobs via `/external-job-api`; complete/fail via `/external-job-api/jobs/{id}` |
| Terminal failure storage | Incident record + failed external task with `retries=0` | Dead-letter job (`management/deadletter-jobs`) |
| Operator retry primitive | Reset task retries (`PUT .../retries`) | Move dead-letter job back (`POST ... {action: move}`) |
| Human-task completion | `POST /task/{id}/complete` | `POST /runtime/tasks/{id}/complete` |
| Management UI | Cockpit (included) | None in OSS (EE-only) |

The benchmark's domain layer (Request/WorkItem/TaskResult/Command) is
identical for both; the differences above are encapsulated inside
`OperatonAdapter` vs `FlowableAdapter`.

## How to reproduce

```bash
make demo-operaton && make incident-operaton   # Operaton, stack left running
make demo-flowable && make incident-flowable   # Flowable
```

Both demos and both incidents run with the daemon worker stopped, so external
task consumption is deterministic and fully controlled by the script.
