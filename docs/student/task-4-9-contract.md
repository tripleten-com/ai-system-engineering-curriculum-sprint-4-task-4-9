# Task 4.9 — Slow query diagnosis contract

`poe audit-trail` reconstructs one interaction from the audit table, and until now that
query has only met a handful of events. This optional Task loads a large supplied history,
measures the reconstruction query, reads its plan to find where the time goes, and makes one
change, an index or a rewrite, whose effect you prove with the database's own plans and
timings. You record six answers in `submission.yaml`. This Task is optional and gates
nothing else in Project 4.

## What is assessed, and by whom

| Assessed | By |
|---|---|
| The pull request changes only the three permitted files and adds at most one new migration | Automated, in this repository (`poe submission`, inside `poe verify`, and `test_submission_change_stays_within_the_permitted_diff`) |
| `submission.yaml` has the published shape: two plan node types, `index` or `rewrite`, two positive medians, `true` or `false`, and not the sample | Automated (`poe answers`, repeated by `poe submission` and `poe verify`) |
| The one change, the new migration's chain and round trip, the trails against the stored baseline, the index-driven read, and your answers against the diff and the plans `poe verify` observes | Automated, in this repository, against the running stack (`poe audit-contract`, inside `poe verify`) |
| The Project 4 controls still pass | Automated (`poe verify`: the running platform, the end-to-end workflow and every test under `tests/student/`); the hosted `security-gate` job runs the settled Task 5 gate |
| Your working notes in `docs/student/audit-query-record.md`, and the plans and benchmark outputs in your pull request description | Nothing grades them; they are the record behind your answers |

This Task has no protected answer check: every answer is checked by `poe verify` in this
repository. Nothing times a query in CI. The median row compares the two numbers you
recorded, which come from your own runs.

## What is already supplied

| Supplied | Where | What it does |
|---|---|---|
| The trail query | `src/common/audit_queries.py` | `trail_for_exception(exception_id)` returns the reconstruction query and its parameters; the audit store runs it for `poe audit-trail`, and the Task 4.9 tools run the same query |
| The audit store | `src/adapters/persistence/audit_store.py` | Reads a trail with whatever `trail_for_exception` returns, and appends the audit sink's records |
| The audit table's index | `migrations/versions/f4c8d2a6b9e1_add_audit_exception_index.py` | One B-tree index on `exception_id`; the head of the supplied migration chain |
| The large history | `poe audit-seed-large`, `src/api/audit_history.py`, `infra/audit/` | 300,000 synthetic audit events with five hand-written sample trails among them; the measurement exception's id is in `README.md` |
| The stored baseline | `infra/audit/trail-baseline.json` | Each sample trail as the supplied query returns it |
| The tools | `poe audit-explain`, `poe audit-benchmark`, `poe audit-trail-compare`, `poe audit-index-size`, `poe rebuild-api`, `poe migrate-new`, `poe migrate`, `poe audit-verify` | See `README.md` |
| The notes template | `docs/student/audit-query-record.md` | One section per Step; nothing assesses it |

Of the supplied pieces above, you may change `src/common/audit_queries.py` and
`docs/student/audit-query-record.md`, add one new file in `migrations/versions/`, and
complete `submission.yaml`. Every other listed file stays as supplied: the public check
reports a change to any of them, or to any existing migration, as a boundary violation.

## Commands

```shell
poe audit-seed-large                  # once: load the supplied history
poe audit-benchmark <exception_id>    # the median, before and after your change
poe audit-explain <exception_id>      # the plan, and the audit table's indexes
poe migrate-new -m "<message>"        # for an index: a new revision after the current head
poe rebuild-api                       # after any change, before you measure or migrate
poe migrate                           # for an index: apply it to the stack's database
poe audit-trail-compare               # the sampled trails against the stored baseline
poe audit-index-size                  # the size of each index on the audit table
poe answers                           # the answer sheet's format
poe submission                        # the format plus the permitted files
poe verify                            # the full public path
```

`poe answers` and `poe submission` are static and need no stack. The `audit-*` commands,
`poe migrate` and `poe audit-contract` need the stack running, and read the query and the
migrations the API image was built with.

## Check-list rows and the checks that read them

| Check-list row | Check |
|---|---|
| Exactly one change: one new migration adding one index, or one rewrite of `trail_for_exception` | `test_the_diff_holds_exactly_one_change` (the diff from your merge base holds one new file in `migrations/versions/` and no change to `src/common/audit_queries.py`, or the reverse; your working tree counts, committed or not) and `test_an_index_migration_adds_exactly_one_index` (in `poe verify`'s own database, the upgrade adds one index, on the audit table, and changes no other index or column) |
| An index migration follows the current head and has an upgrade that creates the index and a downgrade that drops it | `test_the_new_migration_follows_the_supplied_head` (its `down_revision` is the supplied head, the chain has one head, and `alembic upgrade head` reaches it) and `test_the_new_migration_upgrades_downgrades_and_upgrades_again` (the downgrade returns the indexes and columns to the supplied head's exactly, and a second upgrade builds the same schema again) |
| `poe audit-explain` shows an index-driven step reading the audit table in place of the step marked in Step 1 | `test_an_index_driven_node_reads_the_audit_table_after_the_change`: in the plan of the supplied query, the one node that reads `audit_events` uses no index; in the plan of your query, the one node that reads it looks its rows up through an index (its plan shows an `Index Cond`). A sort step above it is reported, not judged |
| `poe audit-trail-compare` reports identical trails before and after | `test_the_trail_matches_the_stored_baseline_for_every_sampled_exception`: the supplied query, and then your query read through the audit store as `poe audit-trail` reads it, return each sampled exception's events, asked for by its id as stored and in upper case, exactly as the stored baseline lists them. Your query must also read a longer probe trail, with events recorded in the same second, exactly as the supplied query reads it, `audit_id` included |
| The median from `poe audit-benchmark` after the change is lower than before | `test_the_recorded_after_median_is_lower_than_the_before_median`: `answers.after_median_ms` is lower than `answers.before_median_ms`. It compares the two numbers you recorded; nothing is timed in CI |
| `answers.before_plan_node` and `answers.after_plan_node` match the plans `poe verify` observes | `test_the_recorded_before_plan_node_matches_the_observed_plan` and `test_the_recorded_after_plan_node_matches_the_observed_plan`. The before answer is compared with the node that reads the audit table in the supplied query's plan; the after answer with the nodes that read it through an index in your query's plan, observed as planned and again with one index access method switched off at a time, so each form the planner could use is accepted. A node's parallel and plain forms are both accepted |
| `answers.change_type` matches your diff | `test_the_recorded_change_type_matches_the_diff` |
| `answers.blocks_writes_during_change` matches how your migration builds the index, or is `false` for a rewrite | `test_the_recorded_write_blocking_matches_how_the_change_is_applied`: the `CREATE INDEX` statement in the SQL Alembic emits for your migration (`alembic upgrade <head>:head --sql`) decides |
| `answers.before_median_ms` and `answers.after_median_ms` use the format the comments in `submission.yaml` describe | `poe answers`, and `test_the_answer_sheet_passes_the_format_check` |
| The Project 4 controls, including the audit events, still pass their checks | `poe smoke`, `poe e2e-tests` and `poe student-tests` inside `poe verify` (`tests/student/test_audit.py` is the audit events' regression), and the hosted `security-gate` job |
| The pull request adds at most one new file in `migrations/versions/`, changes no existing migration, and otherwise modifies only `src/common/audit_queries.py`, `docs/student/audit-query-record.md`, and `submission.yaml` | `poe submission` (the first CI step, repeated inside `poe verify`); `test_submission_change_stays_within_the_permitted_diff` in `tests/contract/test_authoring_contract.py` repeats it under `poe contract` and `poe author-verify` |

Every `test_...` name above, apart from the boundary test, is a row of
`tests/contract/test_audit_query_contract.py`, which `poe audit-contract` runs inside
`poe verify` once the stack is up.

## How `poe verify` observes your change

The runtime rows read one run of `python -m api.audit_lab verify-run` inside the API
container. It never touches the stack's database: it creates a database of its own on the
same server, `coldline_audit_verify`, builds the initializer's schema in it, applies the
supplied migrations up to their head (`f4c8d2a6b9e1`), loads the supplied history, and
plans the supplied query and reads its trails. Then, when `migrations/versions/` holds a new
revision, it upgrades to head; it plans your query and reads its trails through the audit
store; it adds one probe trail and reads it with your query and the supplied one; it prints the SQL Alembic emits for your revision; it downgrades to the supplied head
and upgrades again. Last, it drops the database. Every plan is taken after `ANALYZE`, with
the planner settings `src/api/audit_plan.py` pins. An index you made by hand in your own
database is never there.

`poe audit-verify` prints the same evidence on its own: what your diff holds, each step's
outcome, the node that reads the audit table in each plan, and the trail verdicts.

## Student-editable paths

- `src/common/audit_queries.py`
- `docs/student/audit-query-record.md`
- `submission.yaml`
- one new file directly in `migrations/versions/`

That is the whole list. Before you push, run `git status` and `git diff --stat`: if anything
else changed, or an existing migration changed, the public check reports the boundary
violation rather than your work.
