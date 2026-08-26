# Evidence bundle: 20260825T232057Z-cb35f54

Versioned, immutable compact copy of the authoritative Operaton 2.1.4 vs
Flowable 8.0.0 benchmark run.

- **Run ID**: `20260825T232057Z-cb35f54`
- **Source git SHA**: `cb35f54f5cb8a69f98b6cbdedcb6a6b8bb1ce1c7` (dirty=false)
- **Source run dir** (on disk, git-ignored): `artifacts/runs/20260825T232057Z-cb35f54/`
- **Final repository SHA**: `110f1429242ce6f12ad6f5aba4da3265776e589a` (HEAD of the frozen run; this bundle, the hardened generator and REPORT.md are committed on top of it)

This directory is a compact, intentionally committed copy of the authoritative
evidence. Every file here was verified against the source run's checksum
manifest: 72/73 byte-identical, with `static-checks.json` the single regenerated
file (disclosed below).

## What is committed here

Per engine (`operaton/`, `flowable/`):

- `functional-junit.xml`, `fault-junit.xml`, `audit-junit.xml`
- `resource-metrics.json`
- `fault-scenarios/*.json` (a01a..a02c, r1..r5, stress-smoke, summary,
  ops_demo_demo/incident)
- `api-evidence/*.json` (engine info, deployments, process definitions, running
  instances, timer jobs, external jobs, dead-letter jobs, t07/t09 restart traces)
- `operational/demo.json`, `operational/incident.json`

Top level:

- `manifest.json`, `report-input.json`, `static-checks.json`,
  `unit/unit-junit.xml`

Note: `static-checks.json` was regenerated with the final hardened checker after
the run (finding wording tightened for the fault-injector property and arming
surface; `all_pass` unchanged). All other files are byte-identical copies of the
authoritative run.

## Deliberately unpublished logs

The full run directory also contains runtime logs that are not committed here
(kept on disk in the source run dir; their SHA-256 digests are recorded in the
bundled `sha256sums.txt`):

- `operaton/app.log` (6.9 MB) and `flowable/app.log` (8.3 MB) — application
  server logs
- `engine.log`, `worker.log` (per engine)
- `fault.log`, `durability.log`, `audit.log`, `demo.log`, `incident.log`,
  `resource-measure.log`, `unit/unit.log` (stage logs)
- `environment/*.txt` and `environment/images.json` (runtime environment dumps:
  docker version/ps, python version, uname, free -h, git-info)

These are reproducible runtime logs / environment snapshots, not machine
evidence; omitting them keeps the committed bundle small (520 KB). The non-log
evidence above is the verification surface.

## Regenerating REPORT.md from this bundle

```bash
.venv/bin/python -m scripts.generate_report --run evidence/20260825T232057Z-cb35f54
```

This reproduces the committed `REPORT.md` from the bundle alone; no access to
the source run directory is required.
