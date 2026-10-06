"""Coldline.

===================

File:              src/api/audit_plan.py
Component:         Audit query lab — settings, plans and measurements
Purpose:           Pin the planner settings the Task 4.9 tools plan with, find the plan node
                    that reads the audit table, and classify the SQL a migration emits.
Interacts With:    src/api/audit_lab.py, tests/security/audit_verify.py,
                    tests/contract/test_audit_query_contract.py
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Query plans, deterministic measurement, how an index build takes its lock
Tools:             Python 3.12, PostgreSQL EXPLAIN

Everything here is pure: no database and no clock. ``audit_lab`` applies the settings to
its own session, hands the JSON plans PostgreSQL returns to ``audit_reads``, and hands the
SQL ``alembic upgrade --sql`` prints to ``builds_block_writes``. No node is named here as an
expected answer: a read counts as index-driven because the plan says it looks its rows up
through an index condition, and an answer is compared with the node names the plans
themselves contain.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

AUDIT_TABLE = "audit_events"
# The head revision of the chain Task 4.9 supplies (the audit exception index). A new
# revision must revise it; `poe verify` migrates its own database to it before the seed.
SUPPLIED_HEAD = "f4c8d2a6b9e1"
# Every revision Task 4.9 supplies, oldest first. Anything else in migrations/versions/
# is a revision the student added.
SUPPLIED_REVISIONS: tuple[str, ...] = (
    "0001baseline",
    "c0d895eb5c59",
    "4a1f0c2e9b17",
    "b3d7e9f1c2a4",
    "e5f2a8c4d6b1",
    "f4c8d2a6b9e1",
)
# The prefix of the one line `audit_lab verify-run` prints its JSON report on.
REPORT_MARKER = "AUDIT-VERIFY-REPORT "
# The trail query exactly as Task 4.9 supplies it in src/common/audit_queries.py. `poe
# verify` plans this copy for the plan "before" the change and reads the "before" trail
# with it, so a rewrite of the student's module cannot change what "before" means.
SUPPLIED_TRAIL_SQL = """
        SELECT audit_id, exception_id, event, trace_id, recorded_at, details
        FROM audit_events
        WHERE lower(exception_id) = lower($1)
        ORDER BY recorded_at, audit_id
    """
# Every Task 4.9 tool plans in a session with these settings, so the same table and the
# same statistics give one plan on every machine: no parallel workers, no JIT, a custom
# plan for each execution (the exception id is planned as the value it is), the PostgreSQL
# 16 default costs written out so a changed server default cannot move them, every scan
# and sort method switched on so a database or role setting cannot steer the plan, and a
# statistics target large enough that ANALYZE reads every row of the supplied history
# (300 x 2000 = 600,000 sampled rows, twice the history's size) instead of a random sample.
PLANNER_SETTINGS: tuple[tuple[str, str], ...] = (
    ("max_parallel_workers_per_gather", "0"),
    ("jit", "off"),
    ("plan_cache_mode", "force_custom_plan"),
    ("enable_seqscan", "on"),
    ("enable_indexscan", "on"),
    ("enable_bitmapscan", "on"),
    ("enable_indexonlyscan", "on"),
    ("enable_sort", "on"),
    ("enable_incremental_sort", "on"),
    ("seq_page_cost", "1"),
    ("random_page_cost", "4"),
    ("cpu_tuple_cost", "0.01"),
    ("cpu_index_tuple_cost", "0.005"),
    ("cpu_operator_cost", "0.0025"),
    ("effective_cache_size", "4GB"),
    ("work_mem", "4MB"),
    ("default_statistics_target", "2000"),
)
# The plan "after" the change is observed as planned, and again with one index access
# method switched off at a time, so every index-driven form the planner would use for this
# query on this schema is a node `poe verify` observed. A student's plan and `poe verify`'s
# can pick different forms of the same index read; each observed form is accepted.
INDEX_FORM_VARIANTS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("as planned", ()),
    ("bitmap scans off", (("enable_bitmapscan", "off"),)),
    ("index scans off", (("enable_indexscan", "off"),)),
    ("index-only scans off", (("enable_indexonlyscan", "off"),)),
)
# Plan nodes that order their input. One above the audit read is reported, never judged.
SORTING_NODES = frozenset({"Sort", "Incremental Sort"})
_CREATE_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b(?P<rest>[^;]*)", re.IGNORECASE | re.DOTALL
)
_CONCURRENTLY = re.compile(r"\s*CONCURRENTLY\b", re.IGNORECASE)
# Alembic's offline SQL carries the revision message as a `--` comment line, so a message
# such as "create index on the trail" must not be read as a statement.
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True)
class AuditRead:
    """One plan node that reads the audit table itself.

    ``node`` is the node type as EXPLAIN's text format prints it at the start of the line,
    ``Parallel`` included; ``index`` names the index the read goes through (its own, or a
    bitmap index scan's beneath it), and is None for a read that uses no index;
    ``sorted_above`` says whether a sorting node sits above it; ``index_cond`` says whether
    the read, or a bitmap index scan beneath it, looks rows up with an ``Index Cond``.
    """

    node: str
    index: str | None
    sorted_above: bool
    index_cond: bool = False

    @property
    def index_driven(self) -> bool:
        """Return whether the read finds the table's rows through an index condition.

        A read that walks a whole index and drops rows with a ``Filter`` names an index but
        still reads every entry, so it is not index-driven.
        """
        return self.index is not None and self.index_cond

    def as_dict(self) -> dict[str, object]:
        """Return the read as plain JSON-compatible values."""
        return {
            "node": self.node,
            "index": self.index,
            "index_cond": self.index_cond,
            "index_driven": self.index_driven,
            "sorted_above": self.sorted_above,
        }


def node_name(node: Mapping[str, Any]) -> str:
    """Return a JSON plan node's type as the text format prints it."""
    name = str(node.get("Node Type", ""))
    if node.get("Parallel Aware"):
        name = f"Parallel {name}"
    if node.get("Scan Direction") == "Backward":
        name = f"{name} Backward"
    return name


def _root(document: object) -> Mapping[str, Any]:
    """Return the top plan node of one ``EXPLAIN (FORMAT JSON)`` result."""
    if isinstance(document, list) and document and isinstance(document[0], Mapping):
        document = document[0]
    if isinstance(document, Mapping):
        plan = document.get("Plan")
        if isinstance(plan, Mapping):
            return plan
    raise ValueError("not an EXPLAIN (FORMAT JSON) document")


def _children(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return a plan node's child nodes."""
    plans = node.get("Plans")
    if not isinstance(plans, list):
        return []
    return [child for child in plans if isinstance(child, Mapping)]


def _descendants(node: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield every node below ``node``, depth first."""
    for child in _children(node):
        yield child
        yield from _descendants(child)


def _index_of(node: Mapping[str, Any]) -> str | None:
    """Return the index a table read goes through, or None when it uses none."""
    own = node.get("Index Name")
    if isinstance(own, str) and own:
        return own
    names = sorted(
        {
            str(child["Index Name"])
            for child in _descendants(node)
            if isinstance(child.get("Index Name"), str) and child["Index Name"]
        }
    )
    return ", ".join(names) if names else None


def _has_index_cond(node: Mapping[str, Any]) -> bool:
    """Return whether a table read, or a bitmap index scan beneath it, has an Index Cond."""
    if node.get("Index Cond"):
        return True
    return any(
        child.get("Node Type") == "Bitmap Index Scan" and bool(child.get("Index Cond"))
        for child in _descendants(node)
    )


def audit_reads(document: object, table: str = AUDIT_TABLE) -> list[AuditRead]:
    """Return every node of one plan that reads ``table`` itself, top to bottom."""
    reads: list[AuditRead] = []

    def walk(node: Mapping[str, Any], sorted_above: bool) -> None:
        if node.get("Relation Name") == table:
            reads.append(
                AuditRead(node_name(node), _index_of(node), sorted_above, _has_index_cond(node))
            )
        sorts_here = sorted_above or str(node.get("Node Type", "")) in SORTING_NODES
        for child in _children(node):
            walk(child, sorts_here)

    walk(_root(document), False)
    return reads


def accepted_forms(names: Iterable[str]) -> frozenset[str]:
    """Return every accepted spelling of the given node names.

    One node's parallel and plain forms are the same step read by more or fewer workers,
    and a backward index scan is the same node read in the other direction, so each name
    is accepted with and without ``Parallel`` and without ``Backward``.
    """
    forms: set[str] = set()
    for name in names:
        base = name.removeprefix("Parallel ").removesuffix(" Backward")
        if base:
            forms.update({name, base, f"Parallel {base}"})
    return frozenset(forms)


def median_ms(seconds: Sequence[float]) -> float:
    """Return the median of timings in seconds as milliseconds, to three decimals."""
    if not seconds:
        raise ValueError("no timings to take the median of")
    return round(statistics.median(seconds) * 1000.0, 3)


def index_builds(sql: str) -> list[str]:
    """Return every CREATE INDEX statement in SQL text, as written."""
    return [match.group(0).strip() for match in _CREATE_INDEX.finditer(_SQL_COMMENT.sub("", sql))]


def builds_block_writes(sql: str) -> bool | None:
    """Return whether applying the SQL builds an index while blocking writes to its table.

    A plain ``CREATE INDEX`` holds a lock that blocks inserts, updates and deletes on the
    table until the build ends; ``CREATE INDEX CONCURRENTLY`` does not. Any plain build
    blocks; only concurrent builds do not; with no index build at all, the answer is None.
    """
    builds = list(_CREATE_INDEX.finditer(_SQL_COMMENT.sub("", sql)))
    if not builds:
        return None
    return any(not _CONCURRENTLY.match(build.group("rest")) for build in builds)
