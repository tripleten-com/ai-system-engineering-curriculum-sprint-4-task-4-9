"""Coldline.

===================

File:              tests/contract/test_audit_query_contract.py
Component:         Contract tests — Slow query diagnosis (Add-On Task 4.9)
Purpose:           One assessed check per automatable Check-list row: the one change in the diff,
                    the new migration's chain and round trip, the trail against the stored
                    baseline, the index-driven read, and the six answers against the diff and
                    the plans this run observed.
Interacts With:    tests/security/audit_verify.py, src/api/audit_lab.py (inside the API
                    container), src/api/audit_plan.py, tests/contract/submission_validation.py
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          A plan as evidence, one change with one explanation, reversible migrations
Tools:             Python 3.12, pytest, Docker Compose, Git

``poe audit-contract`` runs this module inside ``poe verify``, after the stack is up. The
rows that read the diff and the answer sheet are static. The rows marked ``runtime`` read one
report, made once for the module by ``api.audit_lab verify-run`` inside the API container: a
database of its own, created fresh beside yours (never yours), the initializer's schema, the
supplied migrations up to their head, the supplied large history, the plan and the trails
of the supplied query, then every new migration upgraded, the plan and the trails of your
query, the migration downgraded and upgraded again, and the database dropped. Nothing here
times a query: the median row compares the two numbers you recorded.

A fresh starter fails the diff row, the answer rows and the index-driven row, and passes the
migration rows and the trail row, which have nothing to judge until a migration or a
rewrite exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from api.audit_plan import AuditRead, accepted_forms, builds_block_writes
from tests.contract.submission_validation import (
    SubmissionError,
    _load_one_document,
    validate_submission,
)
from tests.security import audit_verify
from tests.security.audit_verify import ChangeReport

pytestmark = pytest.mark.assessed

TASK_ROOT = Path(__file__).resolve().parents[2]
AUDIT_TABLE = "audit_events"
EXPLAIN_HINT = "run `poe audit-explain <exception_id>` with the id in README.md and read the plan"


@pytest.fixture(scope="module")
def change() -> ChangeReport:
    """Classify the diff from the starting checkpoint once for the module."""
    return audit_verify.change_report(TASK_ROOT)


@pytest.fixture(scope="module")
def observed() -> Iterator[dict[str, Any]]:
    """Run the fresh-database procedure once for the module and share its report."""
    report = audit_verify.run_lab(TASK_ROOT)
    if report.get("fatal"):
        steps = report.get("steps") or {}
        detail = "; ".join(
            f"{name}: {step.get('detail', '')[-600:]}"
            for name, step in steps.items()
            if not step.get("ok")
        )
        errors = "; ".join(f"{name}: {error}" for name, error in report["errors"].items())
        pytest.fail(f"{report['fatal']}. {detail} {errors}".strip(), pytrace=False)
    yield report


def _answers() -> dict[str, Any]:
    """Return the answer sheet's answers mapping, or an empty mapping when it cannot be read."""
    try:
        document = _load_one_document(TASK_ROOT / "submission.yaml")
    except SubmissionError:
        return {}
    answers = document.get("answers")
    return answers if isinstance(answers, dict) else {}


def _reads(observed: dict[str, Any], key: str) -> list[AuditRead]:
    """Return one observed plan's reads of the audit table, failing the row when none was made."""
    plan = observed.get(key)
    if not isinstance(plan, dict):
        error = observed["errors"].get(key, "no plan was observed")
        pytest.fail(f"the {key.replace('_', ' ')} could not be observed: {error}", pytrace=False)
    return [
        AuditRead(
            str(read["node"]),
            read.get("index"),
            bool(read.get("sorted_above")),
            bool(read.get("index_cond")),
        )
        for read in plan["reads"]
    ]


def _state(observed: dict[str, Any], key: str) -> dict[str, Any]:
    """Return one observed schema state, failing the row when it was not observed."""
    state = observed.get(key)
    if not isinstance(state, dict):
        error = observed["errors"].get(key, "the run did not reach this point")
        pytest.fail(f"the {key.replace('_', ' ')} could not be read: {error}", pytrace=False)
    return state


def _step(observed: dict[str, Any], name: str) -> dict[str, Any]:
    """Return one Alembic step's outcome."""
    step = observed["steps"].get(name)
    return step if isinstance(step, dict) else {"ok": False, "detail": "the step did not run"}


def _new_revisions(observed: dict[str, Any]) -> dict[str, Any]:
    """Return the revisions in migrations/versions/ that Task 4.9 did not supply."""
    chain = observed.get("chain") or {}
    if chain.get("error"):
        pytest.fail(f"Alembic cannot read migrations/versions/: {chain['error']}", pytrace=False)
    revisions = chain.get("new_revisions") or {}
    return revisions if isinstance(revisions, dict) else {}


# --- The one change -------------------------------------------------------------------------


def test_the_diff_holds_exactly_one_change(change: ChangeReport) -> None:
    """One new migration, or a changed `trail_for_exception`, and not both or neither."""
    assert change.history, "no student history to diff: run this in your Task repository"
    changed = ", ".join(change.changed_migrations)
    assert not changed, (
        f"an existing migration changed or was removed ({changed}); restore it and add one "
        "new revision instead"
    )
    assert change.change_type is not None, (
        f"the diff holds {change.describe()}; this Task needs exactly one change: one new "
        "migration that adds one index, or a rewrite of trail_for_exception, not both and "
        "not neither"
    )


# --- The new migration ----------------------------------------------------------------------


@pytest.mark.runtime
def test_the_new_migration_follows_the_supplied_head(observed: dict[str, Any]) -> None:
    """A new revision revises the supplied head, leaves one head, and upgrades to it."""
    new = _new_revisions(observed)
    if not new:
        return
    head = observed["supplied_head"]
    assert len(new) == 1, f"migrations/versions/ holds {len(new)} new revisions: {sorted(new)}"
    ((revision, down_revision),) = new.items()
    assert down_revision == head, (
        f"revision {revision} revises {down_revision!r}, not the current head {head}; "
        "create it with `poe migrate-new` so Alembic sets down_revision to the head"
    )
    assert observed["chain"]["heads"] == [revision], (
        f"the chain has the heads {observed['chain']['heads']}, not {revision} alone"
    )
    upgrade = _step(observed, "upgrade")
    assert upgrade["ok"], f"`alembic upgrade head` failed on a fresh database:\n{upgrade['detail']}"
    assert _state(observed, "state_after_upgrade")["versions"] == [revision]


@pytest.mark.runtime
def test_an_index_migration_adds_exactly_one_index(observed: dict[str, Any]) -> None:
    """The upgrade adds one index on the audit table and changes nothing else in the schema."""
    new = _new_revisions(observed)
    before = _state(observed, "state_supplied")
    after = _state(observed, "state_after_upgrade")
    added = sorted(set(after["indexes"]) - set(before["indexes"]))
    removed = sorted(set(before["indexes"]) - set(after["indexes"]))
    redefined = sorted(
        name
        for name in set(before["indexes"]) & set(after["indexes"])
        if before["indexes"][name] != after["indexes"][name]
    )
    assert after["columns"] == before["columns"], "the upgrade changed a table's columns"
    assert not removed, f"the upgrade removed the index(es) {removed}"
    assert not redefined, f"the upgrade redefined the index(es) {redefined}"
    if not new:
        assert not added, f"the schema gained {added} without a new migration"
        return
    assert len(added) == 1, f"the upgrade adds {len(added)} indexes ({added}); add exactly one"
    table = after["indexes"][added[0]]["table"]
    assert table == AUDIT_TABLE, f"the new index {added[0]} is on {table}, not on {AUDIT_TABLE}"


@pytest.mark.runtime
def test_the_new_migration_upgrades_downgrades_and_upgrades_again(
    observed: dict[str, Any],
) -> None:
    """Downgrade returns the schema to the supplied head exactly; upgrade then works again."""
    if not _new_revisions(observed):
        return
    head = observed["supplied_head"]
    before = _state(observed, "state_supplied")
    upgraded = _state(observed, "state_after_upgrade")
    downgrade = _step(observed, "downgrade")
    assert downgrade["ok"], f"`alembic downgrade {head}` failed:\n{downgrade['detail']}"
    downgraded = _state(observed, "state_after_downgrade")
    assert downgraded["versions"] == [head], (
        f"after the downgrade the database is at {downgraded['versions']}, not {head}"
    )
    left = sorted(set(downgraded["indexes"]) - set(before["indexes"]))
    assert not left, f"the downgrade left the index(es) {left} in place; it must drop them"
    assert downgraded["indexes"] == before["indexes"], "the downgrade did not restore the indexes"
    assert downgraded["columns"] == before["columns"], "the downgrade changed a table's columns"
    reupgrade = _step(observed, "reupgrade")
    assert reupgrade["ok"], (
        f"`alembic upgrade head` failed after the downgrade:\n{reupgrade['detail']}"
    )
    again = _state(observed, "state_after_reupgrade")
    assert again["indexes"] == upgraded["indexes"], "the second upgrade built a different schema"
    assert again["versions"] == upgraded["versions"]


# --- The trail -------------------------------------------------------------------------------


@pytest.mark.runtime
def test_the_trail_matches_the_stored_baseline_for_every_sampled_exception(
    observed: dict[str, Any],
) -> None:
    """The same events in the same order for every sampled exception, before and after."""
    before = observed.get("trails_before")
    assert isinstance(before, dict), observed["errors"].get("trails_before", "not observed")
    broken = {name: findings for name, findings in before.items() if findings}
    assert not broken, (
        f"the supplied query does not reproduce the stored baseline ({broken}); the "
        "checkpoint needs attention, not your submission"
    )
    after = observed.get("trails_after")
    assert isinstance(after, dict), (
        f"your query's trails could not be read through the audit store: "
        f"{observed['errors'].get('trails_after', 'not observed')}"
    )
    different = {name: findings for name, findings in after.items() if findings}
    assert not different, (
        "your query returns a different trail than the supplied query for "
        + "; ".join(f"{name}: {'; '.join(findings)}" for name, findings in different.items())
    )


# --- The plan --------------------------------------------------------------------------------


@pytest.mark.runtime
def test_an_index_driven_node_reads_the_audit_table_after_the_change(
    observed: dict[str, Any],
) -> None:
    """The supplied query's read uses no index; after the change, one node reads through one.

    A sort step above the read is reported, not judged: an index that matches only the
    filter leaves one, and so may the plan for an index that matches the order too.
    """
    before = _reads(observed, "plan_before")
    assert len(before) == 1 and not before[0].index_driven, (
        "the supplied query's plan does not read the audit table once without an index; "
        "the checkpoint needs attention, not your submission"
    )
    after = _reads(observed, "plan_after")
    assert len(after) == 1, (
        f"after your change the plan reads {AUDIT_TABLE} {len(after)} times; the query reads "
        f"one exception's events from it once"
    )
    sort = "with a sort step above it" if after[0].sorted_above else "with no sort step above it"
    how = (
        f"through {after[0].index} with no index condition, so every entry is read"
        if after[0].index
        else "without an index"
    )
    assert after[0].index_driven, (
        f"after your change the plan still reads {AUDIT_TABLE} {how} ({sort}); {EXPLAIN_HINT}"
    )


# --- The answers -----------------------------------------------------------------------------


def test_the_answer_sheet_passes_the_format_check() -> None:
    """The six answers use the format the comments in `submission.yaml` describe."""
    try:
        validate_submission(
            TASK_ROOT / "submission.yaml",
            TASK_ROOT / "docs/contracts/submission.schema.json",
            sample_path=TASK_ROOT / "submission-sample.yaml",
        )
    except SubmissionError as exc:
        pytest.fail(str(exc), pytrace=False)


def test_the_recorded_change_type_matches_the_diff(change: ChangeReport) -> None:
    """`answers.change_type` names the one change the diff holds."""
    recorded = _answers().get("change_type")
    assert change.change_type is not None, (
        f"the diff holds {change.describe()}, which is not one change to name"
    )
    assert recorded == change.change_type, (
        f"answers.change_type is {recorded!r}, but the diff holds {change.describe()}"
    )


def test_the_recorded_after_median_is_lower_than_the_before_median() -> None:
    """The after median you recorded is lower than the before median you recorded."""
    answers = _answers()
    before = answers.get("before_median_ms")
    after = answers.get("after_median_ms")
    numbers = all(
        isinstance(value, int | float) and not isinstance(value, bool) and value > 0
        for value in (before, after)
    )
    assert numbers, "record both medians as positive numbers of milliseconds first"
    assert after < before, (
        f"answers.after_median_ms ({after}) is not lower than answers.before_median_ms "
        f"({before}); benchmark the same exception again after your change"
    )


@pytest.mark.runtime
def test_the_recorded_before_plan_node_matches_the_observed_plan(
    observed: dict[str, Any],
) -> None:
    """`answers.before_plan_node` names the node that reads the audit table before the change."""
    recorded = _answers().get("before_plan_node")
    names = [read.node for read in _reads(observed, "plan_before")]
    assert recorded in accepted_forms(names), (
        f"answers.before_plan_node is {recorded!r}, which is not the node that reads "
        f"{AUDIT_TABLE} in the plan `poe verify` observed for the supplied query; {EXPLAIN_HINT}"
    )


@pytest.mark.runtime
def test_the_recorded_after_plan_node_matches_the_observed_plan(
    observed: dict[str, Any],
) -> None:
    """`answers.after_plan_node` names an index-driven node `poe verify` observed for your query.

    The plan is observed as planned and again with one index access method switched off at
    a time, so each form the planner would read your index with is accepted.
    """
    recorded = _answers().get("after_plan_node")
    plan = observed.get("plan_after")
    forms = plan.get("index_forms", []) if isinstance(plan, dict) else []
    assert recorded in accepted_forms(forms), (
        f"answers.after_plan_node is {recorded!r}, which is not a node `poe verify` observed "
        f"reading {AUDIT_TABLE} through an index for your query; {EXPLAIN_HINT}"
    )


@pytest.mark.runtime
def test_the_recorded_write_blocking_matches_how_the_change_is_applied(
    change: ChangeReport, observed: dict[str, Any]
) -> None:
    """`answers.blocks_writes_during_change` agrees with how the migration builds its index.

    A rewrite builds nothing, so its answer is false. For a migration, the index build in
    the SQL Alembic emits for it (`alembic upgrade <head>:head --sql`) decides, as
    `api.audit_plan.builds_block_writes` reads it.
    """
    recorded = _answers().get("blocks_writes_during_change")
    assert isinstance(recorded, bool), "record true or false in answers.blocks_writes_during_change"
    if not _new_revisions(observed):
        assert change.change_type != "index", "the new migration did not reach the database"
        assert recorded is False, (
            "answers.blocks_writes_during_change is true, but your change builds no index; "
            "a rewrite blocks no writes"
        )
        return
    sql = observed.get("offline_sql")
    printed = _step(observed, "offline_sql")["detail"]
    assert isinstance(sql, str), f"Alembic could not print your migration's SQL:\n{printed}"
    blocks = builds_block_writes(sql)
    assert blocks is not None, "your migration's SQL builds no index"
    assert recorded is blocks, (
        f"answers.blocks_writes_during_change is {str(recorded).lower()}, which is not how "
        "the index build your migration runs treats writes; read the CREATE INDEX statement "
        "in `alembic upgrade --sql` output and what PostgreSQL's documentation says it locks"
    )
