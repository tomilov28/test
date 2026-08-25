# Resource Methodology (Part B)

This document describes how the benchmark measures memory (RSS) and CPU for
the two workflow engines, why the numbers are comparable, and where the raw
evidence lives. It follows the clean-DB measurement protocol: only PostgreSQL
and the target engine run during a measurement; the FastAPI harness and workers
are stopped so no benchmark process contributes to the engine container's
numbers.

Implementation: `scripts/measure_resources.py`, driven by
`make measure-resources`. Evidence: `artifacts/<engine>/resources.json`.

## Protocol

1. **Isolation.** Only the shared `workflow-benchmark-postgres-1` container and
   the target engine container may run. The script checks that
   * the target engine container is up,
   * the *other* engine container is down,
   * no `uvicorn app.main:app` and no `scripts.worker` host process is alive.
   Any violation marks the run `INVALID` and the result
   `NOT STRICTLY COMPARABLE`.

2. **Settle.** The engine container idles for 60s so JIT warm-up, deployment
   scans and startup churn have settled before sampling begins.

3. **Sampling.** `docker stats --no-stream` is sampled 6 times every 10s.
   RSS (bytes) and CPU% are parsed from each sample.

4. **Summary.** Median and min/max RSS, median and max CPU are reported.

5. **Parity.** Both engines run under the same limits:
   * `cpus: 2` (both compose files),
   * `mem_limit: 1024m` (both compose files - Operaton was raised from 512m so
     the two engines are compared under identical caps).
   The script verifies the observed `MemUsage` limit equals 1024 MiB on every
   sample and that the CPU cap is 2. If isolation or any parity condition is
   not met, the verdict is `NOT STRICTLY COMPARABLE`.

6. **Identity.** For each engine the script records `container_id`,
   `image_id`, `repo_digest` (authoritative pinning, audit A12), `started_at`
   and the engine probe response, so the measured numbers can be attributed to
   the exact image.

## Execution

`make measure-resources` performs, in order:

```bash
make stackctl stop-app        # stop the FastAPI harness
make stackctl stop-worker OPERATON
make measure_resources OPERATON   # 60s settle + 6 samples -> artifacts/operaton/resources.json
# stop Operaton, start Flowable, deploy fixtures
make measure_resources FLOWABLE   # -> artifacts/flowable/resources.json
```

Both engines are measured on the same host, back-to-back, so host-level
variation (kernel, cgroup, CPU governor) applies equally to both.

## Verdict rule

| Condition | Result |
| --------- | ------ |
| Isolation valid AND cpu/mem limits equal | `STRICTLY COMPARABLE` |
| Either condition unmet | `NOT STRICTLY COMPARABLE` |

The verdict is recorded in `resources.json` under `parity.verdict`; it is
computed by the script, not hand-written, and the sampled raw rows are included
so the reader can reproduce the median themselves.

## Results (Operaton 2.1.4 vs Flowable 8.0.0)

Recorded from `artifacts/<engine>/resources.json` (clean-DB protocol, both
engines measured back-to-back on the same host with app + workers stopped).

| Engine | Image digest (repo) | Median RSS | RSS range | Median CPU | Verdict |
| ------ | ------------------- | ---------- | ---------- | ---------- | ------- |
| Operaton 2.1.4 | `operaton/operaton@sha256:590c837c…a72410` | 348.4 MiB | 345.8–349.5 MiB | 0.2% | STRICTLY COMPARABLE |
| Flowable 8.0.0 | `flowable/flowable-rest@sha256:708dfa32…25672d` | 285.0 MiB | 284.8–285.3 MiB | 0.3% | STRICTLY COMPARABLE |

Full records (including every sample row, container id/image id, `started_at`
and engine probe): `artifacts/operaton/resources.json`,
`artifacts/flowable/resources.json`.

## Caveats

- RSS is the container's resident set as reported by `docker stats`; it
  includes the JVM heap actually touched, not the configured heap cap.
- CPU% is relative to one core (100% = one vCPU); with `cpus=2` both engines
  can use up to 200%.
- These are idle-baseline numbers (no workload). They characterize each
  engine's standing footprint under identical limits, which is the point of
  the clean-DB protocol; they are not a throughput benchmark.
