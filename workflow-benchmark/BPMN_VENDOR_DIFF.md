# BPMN Vendor Diff — Operaton 2.1.4 vs Flowable 8.0.0

Benchmark fixtures (`LONG_VISIT_POC` v1/v2, plus `credit_decision`) express an
identical logical contract on both engines:

```
START -> external prisoner-data load -> parallel human checks -> parallel join
      -> PT15S durable timer -> final_decision -> END
```

This file documents what actually differs in the BPMN XML and in the
operational/retry mechanics.

## Namespaces & extensions

| | Operaton | Flowable |
| --- | --- | --- |
| Vendor namespace | `xmlns:operaton="http://operaton.org/schema/1.0/bpmn"` | `xmlns:flowable="http://flowable.org/bpmn"` |
| Process-level extension | `operaton:historyTimeToLive="180"` on `<bpmn:process>` | none used (history retention defaults) |
| Service-task extension | `operaton:type="external"` + `operaton:topic="load_prisoner_data"` | `flowable:type="external"` + `flowable:topic="load_prisoner_data"` |
| Definitions `id` | `Definitions_LONG_VISIT_POC_v1` / `_v2` | identical ids |
| `targetNamespace` | `http://benchmark.local/bpmn/long-visit-poc` | identical |

The BPMN *model* (gateways, user tasks, timer, sequence flows, element ids
`finance_check`, `relative_check`, `discipline_check`, `Timer_1`,
`final_decision`) is byte-for-byte the same shape. Only the two vendor
attributes on the external service task and the process attribute differ.

## External worker declaration

Both declare the same external task, but with different attribute names:

```xml
<!-- Operaton -->
<bpmn:serviceTask id="load_prisoner_data" name="Load Prisoner Data"
                  operaton:type="external" operaton:topic="load_prisoner_data" />

<!-- Flowable -->
<bpmn:serviceTask id="load_prisoner_data" name="Load Prisoner Data"
                  flowable:type="external" flowable:topic="load_prisoner_data" />
```

The topic string `load_prisoner_data` is identical. Operaton calls the wait
state an *external task*, Flowable calls it an *external job*; both are polled
from a Python worker on the business-logic side.

## Variables

Fixtures carry no start variables; the external worker sets
`{prisoner_exists: boolean, unit: integer}` on completion and the human
completions pass small dict payloads. No vendor-specific variable types are
used. (Codec difference is confined to the adapters: Flowable uses
`variableDto` with lowercase type names; Operaton uses `VariableValueDto`.)

## Deployment metadata

| | Operaton | Flowable |
| --- | --- | --- |
| Deployment name | `deploymentName` honored | stored as the uploaded file base name; the `deploymentName` form field is **ignored** |
| Versioning | per process key, deployments increment `version` | identical |
| Cascade delete | `DELETE /deployment/{id}?cascade=true` | identical |
| History TTL | set via `operaton:historyTimeToLive` (deployment-level TTL) | default history retention; would use `flowable:historyTimeToLive` if pinned |

## Operational tooling (OSS distribution)

| Capability | Operaton 2.1.4 | Flowable 8.0.0 (`flowable-rest`) |
| --- | --- | --- |
| Web UI | **Cockpit + Tasklist** bundled in the distro | **none** in the `flowable-rest` image (no admin UI; only Swagger at `/flowable-rest/docs`). Flowable's OSS UI ships as a separate `flowable-ui` distribution; Flowable Work (admin UI) is commercial |
| Find process instance | REST `/process-instance`, UI Cockpit | REST `/runtime/process-instances` only |
| Active tasks | REST `/task`; UI Tasklist | REST `/runtime/tasks`, execution tree `/runtime/executions` |
| History | REST `/history/...` | REST `/history/...` |
| Failed jobs | `/job` with retries; incident concept | `/management/deadletter-jobs` (permanent) ; retryable failures stay as external-job rows |
| Retry | external task `retries` + `retryTimeout` job; re-activation after retryTimeout | external job retries; re-activation after the async executor resets the expired lock (PT6S cadence -> ~30-35s wall clock) |
| External topic listing | `GET /external-task/topic-names` | **no equivalent endpoint**; jobs listable via `GET /external-job-api/jobs` |

REST is the only operational mechanism used for Flowable in this benchmark
and is an acceptable operational mechanism per the phase definition.

## Converting the fixtures

**Operaton -> Flowable:** replace the `operaton:` prefix with `flowable:` on
the two service-task attributes and remove `operaton:historyTimeToLive` (or
rename to `flowable:historyTimeToLive`). No structural changes, no gateway /
timer / task edits. ~3 attribute edits per file.

**Flowable -> Operaton:** the reverse: `flowable:` -> `operaton:` and re-add
`operaton:historyTimeToLive` if a deployment TTL is wanted. Same ~3 edits.

The dominant cost is NOT the BPMN XML (trivially mechanical) but the REST
protocol differences between the two engines (external-worker acquire/complete
shapes, deployment-name handling, job/dead-letter query surfaces), which live
entirely inside the two adapters.
