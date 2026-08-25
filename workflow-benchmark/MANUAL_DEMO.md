# Manual Demo (Part C walkthrough)

This is the operator-facing walkthrough. It reproduces, by hand, what
`scripts/ops_demo.py` automates for the `LONG_VISIT_POC` workload on each
engine, and documents the incident response for a terminal external-worker
failure. Run the scripted versions with:

```bash
make demo-operaton && make incident-operaton   # Operaton
make demo-flowable && make incident-flowable   # Flowable
```

Both demos/incidents run with the daemon worker stopped, so external-task
consumption is fully controlled.

---

## 1. Happy path (`--mode demo`)

### Steps

1. Create a request (domain owns `Request`, engine owns BPMN execution):

   ```bash
   curl -s http://localhost:8000/requests \
     -H 'Content-Type: application/json' \
     -d '{"request_type":"LONG_VISIT_POC","request_type_version":1,
          "workflow_engine":"OPERATON",
          "variables":{"initiator":"manual-demo"}}'
   ```

   Response includes `id` (request id) and `number` (the business key). The
   outbox enqueues `START_PROCESS`; the dispatcher starts the engine instance
   and records `workflow_instance_id` on the request.

2. Run the external worker once (topic `load_prisoner_data`):

   ```bash
   .venv/bin/python -m scripts.stackctl start-worker --engine OPERATON
   # wait for the two parallel human tasks to appear
   curl -s http://localhost:8000/requests/<id>/work-items
   ```

   You should see ACTIVE work items `finance_check` and `relative_check`.

3. Complete both parallel human tasks and then `final_decision`:

   ```bash
   # complete finance_check, relative_check, then final_decision = APPROVE
   # (POST /work-items/<work_item_id>/complete with {"data": {...}})
   ```

4. Observe the request reach `CLOSED/COMPLETED`:

   ```bash
   curl -s http://localhost:8000/requests/<id> | jq '{lifecycle_state, outcome}'
   ```

### Management surfaces

| Engine | URL | Credentials |
| ------ | --- | ----------- |
| Operaton | Cockpit http://localhost:8080/operaton/app/cockpit/ , Tasklist http://localhost:8080/operaton/app/tasklist/ , REST http://localhost:8080/engine-rest | `demo` / `demo` |
| Flowable | REST http://localhost:8081/flowable-rest/service , external-job API http://localhost:8081/flowable-rest/external-job-api | `rest-admin` / `test` |

### Expected result

Request `CLOSED`, outcome `COMPLETED`; engine instance ended normally with the
`final_decision` user task approved. Evidence:
`artifacts/<engine>/faults/ops_demo_demo.json`.

---

## 2. Terminal external-worker failure (`--mode incident`)

The external worker reports a **technical** failure on `load_prisoner_data`
with `retries=0`, so the process is stuck on that activity and never reaches a
business decision. This is the durable-workflow worst case: the domain request
must remain `ACTIVE` with outcome `None`, and the engine must *store* the
failure where an operator can find it.

### Diagnosis (engine REST only)

1. Find the process: `GET /process-instance?businessKey=<number>` (Operaton) /
   `GET /service/runtime/process-instances` (Flowable).
2. Check the stuck activity: `GET /process-instance/{id}/activity-instances`.
3. Read the stored failure:
   - Operaton: `GET /incident?processInstanceId=<id>` and `GET /job?processInstanceId=<id>`
     (exception `incident failedExternalTask on load_prisoner_data`, `retries=0`).
   - Flowable OSS: `GET /service/management/deadletter-jobs?processInstanceId=<id>`.
4. Confirm the domain is unharmed: `GET /requests/<id>` shows
   `lifecycle_state: ACTIVE`, `outcome: null`.

### Operator recovery

- Operaton: reset the task retries, then the worker completes it.

  ```bash
  curl -s -X PUT http://localhost:8080/engine-rest/external-task/<task_id>/retries \
    -H 'Content-Type: application/json' -d '{"retries":1}'
  ```

  (Cockpit equivalent: Process Instances -> instance -> Incidents tab ->
  Retry.) Flowable OSS: move the dead-letter job back to the normal queue, then
  the worker completes it.

  ```bash
  curl -s -u rest-admin:test -X POST \
    http://localhost:8081/flowable-rest/service/management/deadletter-jobs/<job_id> \
    -H 'Content-Type: application/json' -d '{"action":"move"}'
  ```

5. Walk the process to completion (parallel checks -> timer -> APPROVE) as in
   the happy path. The request ends `CLOSED/COMPLETED`.

### Expected result

The domain outcome was never decided by the engine failure; recovery was a
purely technical engine action; the same request that survived the incident
completes successfully. Evidence:
`artifacts/<engine>/faults/ops_demo_incident.json` (includes the counted REST
actions used for diagnosis).

---

## 3. Domain-first guarantees an operator can observe

- A cancelled request is `CLOSED/CANCELLED` *immediately* (same transaction
  that enqueues the engine cancel), even if the engine is down. The engine
  termination converges in the background and is idempotent.
- A request whose engine instance ends without a domain outcome stays `ACTIVE`
  and the reconciler records an `engine_ended_without_domain_outcome` anomaly
  (`POST /admin/reconcile` then read `/requests`).
- A `FAILED` technical command (outage outlasted the 5-attempt budget) is
  recovered with `POST /admin/commands/<command_id>/requeue`; the domain
  outcome is never re-decided.

See `AUDIT_REMEDIATION.md` (A01/A02/A04) for the design and evidence.
