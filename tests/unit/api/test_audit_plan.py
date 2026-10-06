"""Coldline.

===================

File:              tests/unit/api/test_audit_plan.py
Component:         Unit tests — Audit query lab plans and settings
Purpose:           Prove the plan reader finds the node that reads the audit table, says whether it
                    reads through an index and whether a sort sits above it, and that the
                    write-blocking reading of migration SQL, the accepted node forms, the median
                    and the pinned settings behave as documented.
Interacts With:    src/api/audit_plan.py
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Query plans, deterministic measurement, how an index build takes its lock
Tools:             Python 3.12, pytest

The plans below are hand-written ``EXPLAIN (FORMAT JSON)`` documents over an invented table,
with invented node, relation and index names; none is a plan of the Task's query. Only the
``Bitmap Index Scan`` node type is real, because the plan reader looks for it by name.
"""

from __future__ import annotations

from typing import Any

import pytest

from api import audit_plan
from tests.security import repository

QUERY_MODULE = "src/common/audit_queries.py"

# An invented table every hand-written plan reads, passed to the plan reader explicitly.
TABLE = "example_table"


def _document(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Wrap one plan node the way EXPLAIN (FORMAT JSON) returns it."""
    return [{"Plan": plan, "Planning Time": 0.1}]


def _scan(node_type: str, **fields: Any) -> dict[str, Any]:
    """Return one table-reading node over the invented table."""
    return {"Node Type": node_type, "Relation Name": TABLE, **fields}


def test_a_read_without_an_index_under_a_sort_is_found_and_reported() -> None:
    """A table read with no index name is not index-driven; the sort above it is reported."""
    plan = {"Node Type": "Sort", "Sort Key": ["example_key"], "Plans": [_scan("Example Read")]}

    reads = audit_plan.audit_reads(_document(plan), TABLE)

    assert reads == [audit_plan.AuditRead("Example Read", None, True)]
    assert not reads[0].index_driven


def test_a_read_through_its_own_index_is_index_driven() -> None:
    """A node that looks rows up through the index it names is index-driven."""
    scan = _scan("Example Index Read", **{"Index Name": "ix_a", "Index Cond": "(x = 1)"})

    reads = audit_plan.audit_reads(_document(scan), TABLE)

    assert reads == [audit_plan.AuditRead("Example Index Read", "ix_a", False, True)]
    assert reads[0].index_driven
    assert reads[0].as_dict() == {
        "node": "Example Index Read",
        "index": "ix_a",
        "index_cond": True,
        "index_driven": True,
        "sorted_above": False,
    }


def test_a_read_of_a_whole_index_with_only_a_filter_is_not_index_driven() -> None:
    """Walking every entry of an index and filtering rows names an index but finds no rows."""
    scan = _scan("Example Index Read", **{"Index Name": "ix_a", "Filter": "(x = 1)"})

    reads = audit_plan.audit_reads(_document(scan), TABLE)

    assert reads == [audit_plan.AuditRead("Example Index Read", "ix_a", False, False)]
    assert not reads[0].index_driven


def test_a_bitmap_read_takes_its_index_from_the_bitmap_scan_beneath_it() -> None:
    """The heap read names no index itself; the bitmap index scan below it does."""
    bitmap = {"Node Type": "Bitmap Index Scan", "Index Name": "ix_b", "Index Cond": "(x = 1)"}
    heap = _scan("Example Heap Read", Plans=[bitmap], **{"Recheck Cond": "(x = 1)"})
    plan = {"Node Type": "Sort", "Plans": [heap]}

    reads = audit_plan.audit_reads(_document(plan), TABLE)

    assert reads == [audit_plan.AuditRead("Example Heap Read", "ix_b", True, True)]
    assert reads[0].index_driven


def test_only_reads_of_the_audit_table_are_returned() -> None:
    """A join's other relation is not a read of the table asked about."""
    other = {"Node Type": "Example Read", "Relation Name": "example_other_table"}
    hashed = {"Node Type": "Example Build", "Plans": [other]}
    plan = {"Node Type": "Example Join", "Plans": [_scan("Example Read"), hashed]}

    reads = audit_plan.audit_reads(_document(plan), TABLE)

    assert [read.node for read in reads] == ["Example Read"]


def test_node_names_follow_the_text_format() -> None:
    """A parallel node is prefixed, a backward scan suffixed, as EXPLAIN's text prints them."""
    assert audit_plan.node_name({"Node Type": "Example Read", "Parallel Aware": True}) == (
        "Parallel Example Read"
    )
    backward = {"Node Type": "Example Index Read", "Scan Direction": "Backward"}
    assert audit_plan.node_name(backward) == "Example Index Read Backward"
    assert audit_plan.node_name({"Node Type": "Example Join"}) == "Example Join"


def test_a_document_that_is_not_a_json_plan_is_refused() -> None:
    """Anything but an EXPLAIN (FORMAT JSON) result is a tooling error, not an empty plan."""
    with pytest.raises(ValueError, match="EXPLAIN"):
        audit_plan.audit_reads({"rows": []})


def test_accepted_forms_admit_the_parallel_and_plain_forms_of_each_name() -> None:
    """Each observed name is accepted with and without Parallel, and without Backward."""
    forms = audit_plan.accepted_forms(["Parallel Example Join", "Example Index Read Backward"])

    assert {
        "Parallel Example Join",
        "Example Join",
        "Example Index Read",
        "Parallel Example Index Read",
    } <= forms
    assert "Example Index Read Backward" in forms
    assert "Example Read" not in forms
    assert audit_plan.accepted_forms([]) == frozenset()


@pytest.mark.parametrize(
    "sql,blocks",
    [
        ("CREATE INDEX ix_a ON t (a);", True),
        ("create unique index ix_a on t (a);", True),
        ("CREATE INDEX IF NOT EXISTS ix_a ON t (a);", True),
        ("COMMIT;\nCREATE INDEX CONCURRENTLY ix_a ON t (a);\nBEGIN;", False),
        ("CREATE UNIQUE INDEX CONCURRENTLY ix_a ON t (a);", False),
        ("CREATE INDEX CONCURRENTLY ix_a ON t (a);\nCREATE INDEX ix_b ON t (b);", True),
        ("UPDATE alembic_version SET version_num='x';", None),
        (
            "-- Running upgrade a -> b, create index on the trail\n"
            "COMMIT;\nCREATE INDEX CONCURRENTLY ix_a ON t (a);\nBEGIN;",
            False,
        ),
        ("-- create index on the trail\nUPDATE alembic_version SET version_num='x';", None),
    ],
    ids=[
        "plain",
        "plain-lowercase",
        "plain-if-not-exists",
        "concurrent",
        "concurrent-unique",
        "one-of-two-plain",
        "no-index",
        "concurrent-under-a-message-comment",
        "comment-only",
    ],
)
def test_the_write_blocking_reading_of_migration_sql(sql: str, blocks: bool | None) -> None:
    """A plain build blocks writes, a concurrent one does not, and no build has no answer."""
    assert audit_plan.builds_block_writes(sql) is blocks


def test_index_builds_lists_every_create_index_statement() -> None:
    """Each CREATE INDEX statement is returned as written, up to its semicolon."""
    sql = "BEGIN;\nCREATE INDEX ix_a ON t (a);\nCREATE INDEX CONCURRENTLY ix_b ON t (b);\n"

    assert audit_plan.index_builds(sql) == [
        "CREATE INDEX ix_a ON t (a)",
        "CREATE INDEX CONCURRENTLY ix_b ON t (b)",
    ]


def test_the_median_is_in_milliseconds_to_three_decimals() -> None:
    """Seconds in, milliseconds out; an empty run is an error."""
    assert audit_plan.median_ms([0.0123, 0.0101, 0.0400]) == 12.3
    assert audit_plan.median_ms([0.001, 0.002]) == 1.5
    with pytest.raises(ValueError, match="no timings"):
        audit_plan.median_ms([])


def test_the_pinned_settings_switch_off_parallelism_and_read_every_row() -> None:
    """One plan per table: no parallel workers, no JIT, custom plans, every method on."""
    settings = dict(audit_plan.PLANNER_SETTINGS)

    assert settings["max_parallel_workers_per_gather"] == "0"
    assert settings["jit"] == "off"
    assert settings["plan_cache_mode"] == "force_custom_plan"
    for switch in (
        "enable_seqscan",
        "enable_indexscan",
        "enable_bitmapscan",
        "enable_indexonlyscan",
        "enable_sort",
        "enable_incremental_sort",
    ):
        assert settings[switch] == "on", switch
    variant_switches = {name for _, extra in audit_plan.INDEX_FORM_VARIANTS for name, _ in extra}
    assert variant_switches <= set(settings), "each variant switches off a pinned method"
    assert int(settings["default_statistics_target"]) * 300 >= 600_000
    assert len(settings) == len(audit_plan.PLANNER_SETTINGS)


def test_the_index_form_variants_switch_off_one_access_method_each() -> None:
    """The first variant is the plan as made; each other switches off one method only."""
    names = [name for name, _ in audit_plan.INDEX_FORM_VARIANTS]
    assert names[0] == "as planned"
    assert audit_plan.INDEX_FORM_VARIANTS[0][1] == ()
    for _, extra in audit_plan.INDEX_FORM_VARIANTS[1:]:
        assert len(extra) == 1
        assert extra[0][0].startswith("enable_") and extra[0][1] == "off"


def test_the_supplied_head_is_the_last_supplied_revision() -> None:
    """A new revision revises the last revision Task 4.9 supplies."""
    assert audit_plan.SUPPLIED_REVISIONS[-1] == audit_plan.SUPPLIED_HEAD
    assert len(set(audit_plan.SUPPLIED_REVISIONS)) == len(audit_plan.SUPPLIED_REVISIONS)


def test_the_trusted_trail_query_is_the_one_the_starting_checkpoint_supplies() -> None:
    """``SUPPLIED_TRAIL_SQL`` is the SQL the starting checkpoint's query module returns.

    The module is read as the starting checkpoint (the merge base) holds it, so a change you
    make to it never fails this test; only a checkout with no Git history reads the file.
    """
    root = repository.TASK_ROOT
    if repository.is_student_checkout(root):
        text = repository.baseline_text(root, QUERY_MODULE)
        assert text is not None, f"the starting checkpoint has no {QUERY_MODULE}"
    else:
        text = (root / QUERY_MODULE).read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    exec(compile(text, QUERY_MODULE, "exec"), namespace)

    sql, parameters = namespace["trail_for_exception"]("x")

    assert " ".join(sql.split()) == " ".join(audit_plan.SUPPLIED_TRAIL_SQL.split())
    assert parameters == ["x"]
