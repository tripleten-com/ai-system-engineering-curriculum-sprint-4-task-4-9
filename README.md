# Coldline Task 4.9 — Optional Task 9: Slow query diagnosis

This checkpoint is the finished Project 4 system from Task 4.7: the audit sink records what
set off each summary, what came back, what was kept and who read it, and `poe audit-trail`
reconstructs one exception's interaction from those records. This Task runs the
reconstruction query against a large supplied audit history, reads its query plan to find
where the time goes, and makes one change, a new index or a rewrite of the query, whose
effect you prove with the plan and with timings, and whose cost you name. You record six
answers in `submission.yaml`. This Task is optional.

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/tripleten-com/ai-system-engineering-curriculum-sprint-4-task-4-9/tree/main)

## Start the system

Prerequisites are Python 3.12 and Docker with Compose v2. The supplied bootstrap supports macOS
arm64/x86-64, Windows x86-64, and Linux x86-64/aarch64, and installs pinned uv 0.11.8 under
`.tools/bin`. If your computer cannot run the stack locally, use the Codespaces button above.

On macOS and most Linux distributions the interpreter is `python3`; substitute it wherever these
commands say `python`.

```shell
python infra/scripts/bootstrap.py
./.tools/bin/uv sync --frozen
./.tools/bin/uv run --frozen poe preflight
./.tools/bin/uv run --frozen poe start
./.tools/bin/uv run --frozen poe ready
```

PowerShell and POSIX wrappers are available under `infra/scripts/`. After uv is on `PATH`, the
shorter `uv run --frozen poe <task>` form works; in PowerShell on Windows the pinned binary is
`.tools/bin/uv.exe`.

| Service | Local URL | Purpose |
|---|---|---|
| API | `http://localhost:8000` | Submit readings, poll exception summaries, search procedures |
| Token issuer: discovery document | `http://localhost:8180/.well-known/openid-configuration` | The development issuer's OIDC discovery document: its `issuer` and `jwks_uri` |
| Token issuer: key set | `http://localhost:8180/.well-known/jwks.json` | The published key set (JWKS) the settled `config/auth.yaml` names |
| Jaeger | `http://localhost:16686` | Open traces; the trace ids the audit records carry are these |
| Grafana | `http://localhost:3000` | Use the focused diagnostics dashboard |
| Prometheus | `http://localhost:9090` | Query bounded metrics and inspect the deployed alert rule |
| Alertmanager | `http://localhost:9093` | Inspect firing and resolved alerts |
| LocalStack S3/SQS/Secrets Manager | `http://localhost:4566` | The emulated object-storage, queue and secret-store endpoint |

Each of these ports can be overridden by setting the matching `COLDLINE_API_HOST_PORT`,
`COLDLINE_ISSUER_HOST_PORT`, `COLDLINE_JAEGER_HOST_PORT`, `COLDLINE_GRAFANA_HOST_PORT`,
`COLDLINE_PROMETHEUS_HOST_PORT`, `COLDLINE_ALERTMANAGER_HOST_PORT`, or
`COLDLINE_LOCALSTACK_HOST_PORT` environment variable in your shell environment or a local
`.env` file (copy `.env.example`) if a default collides with something already running on your
machine. Keep the override in place for every `poe` command. If you remap the issuer port,
keep `config/auth.yaml` unchanged: it is supplied in this Task and names the default port.
Host-side tools (the carried access and audit tests, `poe token-check`) resolve the API's and
the issuer's origins from these variables (the environment, then `.env`, then the default).
This Task's own tools need no host port: they run inside the API container, beside the
database.

This Task runs as its own Compose project, `coldline-task-4-9`. If an earlier Task's stack is
still running, run `poe stop` in that Task's repository first; otherwise `poe start` here fails
because the published ports are already taken.

PostgreSQL, Redis, worker metrics, and OTLP remain inside the Compose network. Codespaces uses the
same `compose.yaml` and keeps every forwarded port private.

## The measurement exception

Every Task 4.9 measurement uses one exception's trail. Its id is:

```text
exc-5c2e8f14-7a3b-5d91-b6e0-4f8a2c9d1e37
```

It is one of five sample exceptions in the supplied history (`infra/audit/`); the commands
below write it as `<exception_id>`.

## Load the large history

```shell
./.tools/bin/uv run --frozen poe audit-seed-large
```

It loads 300,000 synthetic audit events into the `audit_events` table of the running stack,
in one transaction, and prints the number of events loaded and the number the table now
holds. Run again, it finds the history already loaded and leaves it as it is, so it is safe
to repeat. The history lives in the PostgreSQL volume: rebuilding or restarting the API keeps
it, `poe stop` keeps it, and `poe reset` deletes it, after which you load it again with the
same command.

## Rebuild the API container

The API image carries `src/` and `migrations/` as they were when it was built. `poe
audit-explain`, `poe audit-benchmark`, `poe audit-trail-compare`, `poe audit-trail` and
`poe migrate` all run inside the API container, so after you change
`src/common/audit_queries.py` or add a migration, rebuild the image and recreate the API
container before you measure or migrate:

```shell
./.tools/bin/uv run --frozen poe rebuild-api
```

`poe rebuild-api` rebuilds the API image from this checkout and recreates the API container
alone; the database and the history in it stay. `poe restart` restarts the existing
containers **without rebuilding**, so it keeps the code the image was built with.

## Create a migration and apply it

Schema changes go through Alembic, as in Project 2. Create a revision on the host:

```shell
./.tools/bin/uv run --frozen poe migrate-new -m "describe your change"
```

Alembic writes `migrations/versions/<revision>_describe_your_change.py` with `down_revision`
set to the current head, `f4c8d2a6b9e1` (the supplied index on `audit_events.exception_id`),
so your revision follows it. It never connects to the database. Write `upgrade` and
`downgrade` yourself: autogeneration is off (`migrations/env.py` says why), and the downgrade
must reverse exactly what the upgrade did. Alembic runs each migration inside a transaction;
a statement PostgreSQL refuses to run inside a transaction block goes inside
`with op.get_context().autocommit_block():`.

Then rebuild the API container and apply the revision to the stack's database:

```shell
./.tools/bin/uv run --frozen poe rebuild-api
./.tools/bin/uv run --frozen poe migrate
```

`poe migrate` prints `Running upgrade f4c8d2a6b9e1 -> <revision>` when it applies yours.
`poe migrate-current` prints the database's revision and `poe migrate-down` steps back one.
The initializer also brings the schema to the head of the image's migrations on every
`poe start`. Never create an index by hand in `psql`: `poe verify` builds its own database
from the migrations, where a hand-made index never exists.

If `poe start` or `poe verify` stops at the initializer after you add a revision, your
upgrade failed on the stack's database: run `poe rebuild-api` (it starts the API container
without the initializer), then `poe migrate` to see Alembic's error. To change a
revision you already applied, run `poe migrate-down` first, while the API image still holds
the applied version and its `downgrade`; then edit the revision, run `poe rebuild-api`, and
run `poe migrate` again.

To drop a revision you applied (for example, to switch to a rewrite), run `poe migrate-down`
before you delete the file, then `poe rebuild-api`. If you already deleted it, restore it, run
`poe rebuild-api` and `poe migrate-down`, delete it and run `poe rebuild-api` again; or run
`poe reset`, then `poe start` and `poe audit-seed-large` to begin from a fresh database.

## Command path

For this Task, run the supplied commands in this order:

```text
poe audit-seed-large                  # Step 1: once
poe audit-benchmark <exception_id>    # Step 1: the median before any change
poe audit-explain <exception_id>      # Step 1: the plan before any change
poe migrate-new -m "<message>"        # Step 2, for an index: then fill in the revision
poe rebuild-api                       # Step 2: after your change, for an index or a rewrite
poe migrate                           # Step 2, for an index
poe audit-explain <exception_id>      # Step 2: the plan after your change
poe audit-trail-compare               # Step 2
poe audit-benchmark <exception_id>    # Step 3: the median after your change
poe audit-index-size                  # Step 3, for an index
poe answers                           # as you fill submission.yaml
poe verify
```

The exact public command is `./.tools/bin/uv run --frozen poe verify`, run from the repository
root. Where a Task page shortens a command to `poe <task>`, that is the form it means.

| Command | Use |
|---|---|
| `poe verify` | The public student verification path: the answer format and the permitted files, the unit tests, the stack, then `poe audit-contract` in a database of its own (the history, the migrations with yours upgraded, downgraded and upgraded again, the trails against the stored baseline and a probe trail against the supplied query, the index-driven read, your answers against its plans and your diff), then the inherited control checks |
| `poe audit-seed-large` | Load the supplied large audit history into the stack's database, once, and print the event count |
| `poe audit-explain <exception_id>` | Refresh the audit table's statistics, print `EXPLAIN (ANALYZE, BUFFERS)` of `trail_for_exception` for that exception, and list the indexes on the audit table |
| `poe audit-benchmark <exception_id>` | Refresh the statistics, run the query once to warm up and fifteen times more, and print the median in milliseconds (`median_ms`), with the fastest and slowest run |
| `poe audit-trail-compare` | Read the trail of each sampled exception the way `poe audit-trail` does, by its id as stored and in upper case, and compare each reading, event by event, with the stored baseline from the supplied query (`infra/audit/trail-baseline.json`) |
| `poe audit-index-size` | Print the size of each index on the audit table, and the table's own size |
| `poe rebuild-api` | Rebuild the API image from this checkout and recreate the API container alone |
| `poe migrate-new -m "<message>"` | Write a new Alembic revision after the current head, on the host |
| `poe migrate`, `poe migrate-down`, `poe migrate-current` | Step the stack's schema inside the API container, with the image's migrations |
| `poe audit-contract` | The assessed module `poe verify` runs, on its own; needs the running stack |
| `poe audit-verify` | The same evidence printed on its own: what your diff holds, each step of `poe verify`'s own database run, the node that reads the audit table in each plan, and the trail verdicts |
| `poe answers` | The answer sheet's format on its own |
| `poe submission` | The same check plus the permitted-files boundary |
| `poe audit-trail <exception_id>` | Task 3's tool, now reading with `trail_for_exception`: one exception's audit records in order |
| `poe integrity-record`, `poe integrity-check` | The first and last steps of `poe verify`: hash the student files and the checks' own files into a snapshot outside the repository, then compare the tree with it |
| `poe student-tests` | Run the carried Task 4.2, 4.3 and 4.4 tests under `tests/student/`; none is yours in this Task; start the stack first |
| `poe auth-checks`, `poe token-check <fixture>`, `poe auth-config` | Task 2's tools over the settled `config/auth.yaml`, still runnable |
| `poe scenario [--response <name>] [--note <id>]`, `poe pii-scan <exception_id>`, `poe redaction-report`, `poe model-request <exception_id>` | Task 3's and Task 4's tools, still runnable |
| `poe secret-status`, `poe secret-replace`, `poe provider-auth-check`, `poe secret-check-old` | Task 5's secret tools, still runnable; none prints a value |
| `poe security-setup`, `poe security-scan`, `poe seed-secret`, `poe seed-vulnerable` | Task 5's gate tools, kept as supplied; the `security-gate` job runs `poe security-scan` on every pull request |
| `poe queue-contract`, `poe slo-contract`, `poe gate-contract`, `poe runbook-contract`, `poe e2e` | Project 3's checks and the inherited platform checks, runnable as supplied |
| `poe contract` | Check interfaces, boundaries, submissions, and repository structure |
| `poe restart` | Restart the existing API and worker containers **without rebuilding** |
| `poe stop` | Remove containers and the network, keeping named volumes |
| `poe reset` | Remove containers, the network, and local named volumes, the loaded history among them |

Every plan and timing these tools take uses the planner settings `src/api/audit_plan.py`
pins (no parallel workers, no JIT, a plan made for the exception id itself, the default
costs, statistics read from every row), after `ANALYZE`, so one table gives one plan on every
machine.

`poe verify` records an integrity snapshot, runs `poe answers` and `poe submission`, runs the
unit tests, starts the stack (the API image carries your query and your migration), ingests
the supplied corpus, and runs `poe audit-contract`. That step never touches your database: it
creates a database of its own beside it, builds it from the migrations up to the supplied
head, loads the same history, plans the supplied query and reads its trails, applies a new
migration (upgrade, downgrade, upgrade), plans your query and reads its trails, and drops the
database again. It times nothing: the median row compares the two numbers you recorded. Last
it reruns the inherited control checks (`poe smoke`, `poe e2e-tests`, `poe student-tests`)
and compares the tree with the snapshot.

## Folder map

```text
repository root/
├── config/              Retrieval configuration, settled since Sprint 2, and the settled auth.yaml
├── docs/                Student guidance, public contracts, fidelity notes, the security and governance material
│   ├── contracts/       This Task's answer schema
│   ├── fidelity/        Local-runtime boundary notes for each active adapter and the token issuer
│   ├── governance/      The Task 6 material, supplied
│   ├── security/        The supplied workflow, threat catalog, control matrix, access policy, output policy, audit events, redactor, and gate policy
│   ├── architecture/    Supplied vector engine technical profiles, in prose
│   ├── retrieval/       Supplied retrieval pipeline reference
│   └── student/         This Task's contract and your audit-query-record.md notes, the settled threat model, the Task 6 corrections, the Project 3 runbook
├── evidence/            Git-ignored: the evidence file `poe attack-dev` writes, if you run it
├── infra/               Local setup and runtime configuration
│   ├── audit/           The large history's sample trails and the stored baseline of their trails
│   ├── containers/      The API and worker Dockerfiles, with the build identity arguments
│   ├── issuer/          The development token issuer: its server script and the published key set
│   ├── observability/   Prometheus, Alertmanager, and Grafana configuration
│   ├── release/         The supplied release manifest, unchanged
│   ├── corpus/          Supplied synthetic corpus, query set, and investigation
│   ├── judge/           Supplied cached judge evidence and its provenance record
│   ├── profiles/        Supplied engine and emulator profiles, and their provenance record
│   └── postgres/        Database initialization and the migration baseline stamp
├── loadtest/            Supplied traffic profile and provider-latency harness
├── migrations/          Alembic environment, revision template, and revisions; your one new revision goes in versions/
├── reports/             Git-ignored: the scan report and the bill of materials `poe security-scan` writes
├── schemas/             The supplied output schema the guardrail enforces
├── security/            The settled gate thresholds, the scanner pins, the supplied Semgrep rules
├── src/
│   ├── api/             HTTP application code, composition, the initializer, the audit-trail command, and the audit query lab (audit_lab.py, audit_history.py, audit_plan.py)
│   │   └── security/    The settled TokenVerifier and require_access rule
│   ├── worker/          Background application code, the guardrail, the redaction calls
│   ├── common/          The supplied audit sink, the redactor, and the trail query (audit_queries.py)
│   ├── domain/          Shared domain code, contracts, the failure taxonomy, service and repository contracts
│   ├── ports/           Application interfaces
│   └── adapters/        Technology-specific implementations, including the audit store that runs the trail query
└── tests/
    ├── unit/            Isolated behavior checks
    ├── contract/        Interface and repository checks, this Task's slow-query module and answer-sheet checks
    ├── fixtures/        Supplied fixtures: the token fixtures and the earlier Tasks' fixtures
    ├── security/        Supplied tooling: the slow-query evidence reader, the integrity bookends, and the earlier Tasks' tools
    ├── student/         The carried Task 4.2, 4.3 and 4.4 test files; none is yours in this Task
    ├── smoke/           Running-platform checks
    └── e2e/             Supplied workflow tools and checks, including `poe scenario`
```

## Overview

Use the Optional Task 9 lesson (Task 4.9 in this repository) to decide what to do. This README
covers local setup and repository orientation.

1. `README.md` — local setup, the measurement exception, the commands, and permitted changes.
2. [`docs/student/task-4-9-contract.md`](docs/student/task-4-9-contract.md) — what this Task
   assesses and who assesses it, the supplied pieces, the Check-list rows and the checks that
   read them, and the permitted paths.
3. [`src/common/audit_queries.py`](src/common/audit_queries.py) — the query, and what any
   version of it must keep: the six columns, the one exception's events, the total order.
4. [`docs/student/audit-query-record.md`](docs/student/audit-query-record.md) — your working
   notes, one section per Step.
5. [`infra/audit/README.md`](infra/audit/README.md) — what the supplied history holds.

## Test levels

| Level | Requires Compose | Main question |
|---|---|---|
| Unit | No | Does one responsibility behave correctly, including failures? |
| Contract | Some | Do interfaces, schemas, paths, and dependency rules stay compatible? |
| Smoke | Yes | Did the complete local platform initialize and become observable? |
| E2E | Yes | Can an external client complete the supplied workflow, in one trace? |
| Student | Issuer | Do the carried Task 4.2, 4.3 and 4.4 tests still hold over the supplied code? |

Contract checks marked `runtime` need the running stack. `poe contract` skips them and this
Task's assessed module; `poe audit-contract` runs the assessed module. A fresh checkout fails
`poe answers`, so `poe verify` stops there until `submission.yaml` is filled.

## Submission checks

Run `poe verify` locally before opening your student pull request. Public GitHub CI repeats
the student checks, running `poe submission` first so a boundary violation fails fast, and the
`security-gate` job runs `poe security-scan` with the settled thresholds. This Task has no
protected answer check: every answer is compared, in this repository, with the plans
`poe verify` observes or with your diff, and the after median with the before median. Keep the
before and after plans and both `poe audit-benchmark` outputs in your pull request description,
with each command and when you ran it; nothing grades them. This Task is optional and gates
nothing else in Project 4.

## Task boundary

Task 4.9 asks you to make one change, a new migration that adds one index or a rewrite of
`trail_for_exception`, and to record your answers.

The only student-editable paths are:

- `src/common/audit_queries.py`
- `docs/student/audit-query-record.md`
- `submission.yaml`
- one new file directly in `migrations/versions/`

The audit sink, the audit store, the events it records, the query lab and its fixtures, the
existing migrations, the migration environment, the supplied tests and tools, the security
gate and the workflows stay as supplied. The public check compares the diff from your merge
base with these paths and reports any other change, or a change to an existing migration, as
a boundary violation.

### Student walkthrough

See **Optional Task 9: Slow query diagnosis** in your course platform for the full
walkthrough. In outline: start the stack and load the history; benchmark the measurement
exception and read its plan; decide between an index and a rewrite; make the one change,
rebuild the API and, for an index, apply the migration; read the new plan and compare the
trails; benchmark again and work out what the change costs; fill `submission.yaml`; run
`poe verify`; open your pull request with the plans and the benchmark outputs in its
description.

## Operational limits

This is a local development stack. The token issuer is a development service: it publishes
one fixed key set and issues no tokens; the eight fixtures were signed once and committed.
See [TokenIssuer fidelity](docs/fidelity/TokenIssuer.md). The Compose PostgreSQL password and
the LocalStack access keys are development values, listed once more in
`tests/fixtures/credentials/test-values.yaml` so the audit checks can search for them. Never
place real credentials, personal data, or production records in this repository. Every event
of the supplied audit history is synthetic.

A median `poe audit-benchmark` prints is a measurement on your machine, with this fixture and
these planner settings. A production database has a different table size, different
hardware, a different cache and concurrent writes, so its times would differ; the numbers
show the direction and the rough size of a change, not a production latency.

Alertmanager here is configured with a "default" receiver that has no notification integration:
alerts are queryable through its own API but never sent anywhere real. Never add a webhook, email,
Slack, or paid integration; Sprints 1-4 are emulator-only and never call a hosted endpoint.

Named volumes preserve local PostgreSQL, Redis, Prometheus, Alertmanager, Grafana, and Jaeger state
across `poe stop`; the audit table and the loaded history are in the PostgreSQL volume.
LocalStack object, queue and secret contents are deliberately not persisted; the initializer
re-uploads the supplied corpus artifacts, re-provisions the queue and re-creates the secret's
first version on every start. The `poe reset` command deletes the named volumes. This topology
makes no backup, replication, high-availability, disaster-recovery, capacity, latency-SLO, or
availability claim beyond what Project 3 settled.

See [TokenIssuer fidelity](docs/fidelity/TokenIssuer.md),
[JobQueue fidelity](docs/fidelity/JobQueue.md),
[ModelProvider fidelity](docs/fidelity/ModelProvider.md),
[SecretProvider fidelity](docs/fidelity/SecretProvider.md),
[ObjectStore fidelity](docs/fidelity/ObjectStore.md), and
[Retriever fidelity](docs/fidelity/Retriever.md) for the active adapter boundaries. The
[local runtime evidence](docs/fidelity/local-runtime.md) records the current measurement and its
qualification limits.
