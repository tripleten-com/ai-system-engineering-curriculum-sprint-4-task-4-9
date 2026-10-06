"""Coldline.

===================

File:              tests/unit/api/test_audit_lab.py
Component:         Unit tests — Audit query lab trail compare
Purpose:           Prove `poe audit-trail-compare` and `poe verify`'s trail row read each sampled
                    exception by its id as stored and in upper case, and that `poe verify`'s probe
                    trail compares every event, audit_id and order included, with the supplied
                    query's reading.
Interacts With:    src/api/audit_lab.py, src/api/audit_plan.py,
                    src/adapters/persistence/audit_store.py, infra/audit/trail-baseline.json
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Keeping a query's meaning, case-insensitive matching, a total order
Tools:             Python 3.12, pytest

No database: the pool is a stand-in that holds the rows and answers each query by an
invented rule keyed on the text it is sent. The query module is replaced by a stub that
sends an opaque stand-in token, so nothing here depends on the SQL the query module holds;
that SQL is yours to change in Task 4.9.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
import pytest

from adapters.persistence import audit_store
from api import audit_history, audit_lab, audit_plan

RULE_A = "STAND-IN RULE A"
RULE_B = "STAND-IN RULE B"
RULE_C = "STAND-IN RULE C"
Row = dict[str, Any]


def _folded(stored: str, asked: str) -> bool:
    """Match an id without regard to letter case."""
    return stored.lower() == asked.lower()


def _by_time_then_id(row: Row) -> tuple[Any, ...]:
    """Order oldest first, ties by audit_id."""
    return (row["recorded_at"], row["audit_id"])


def _by_time(row: Row) -> tuple[Any, ...]:
    """Order oldest first, ties in the order the rows are stored."""
    return (row["recorded_at"],)


# Each stand-in text's rule: which stored ids it matches, and how it orders the rows.
RULES: dict[str, tuple[Callable[[str, str], bool], Callable[[Row], tuple[Any, ...]]]] = {
    audit_plan.SUPPLIED_TRAIL_SQL: (_folded, _by_time_then_id),
    RULE_A: (_folded, _by_time_then_id),
    RULE_B: (_folded, _by_time),
    RULE_C: (lambda stored, asked: stored == asked, _by_time_then_id),
}


class _Pool:
    """Stand in for an asyncpg pool over stored rows: match, then order, them."""

    def __init__(self, rows: list[Row]) -> None:
        """Hold the table's rows, in the order they are stored."""
        self.rows = rows
        self.asked: list[str] = []

    async def fetch(self, sql: str, *arguments: object) -> list[Row]:
        """Return the rows the stand-in text's rule matches, in its order."""
        asked = str(arguments[0])
        if sql != audit_plan.SUPPLIED_TRAIL_SQL:
            self.asked.append(asked)
        matches, order = RULES[sql]
        matched = [row for row in self.rows if matches(row["exception_id"], asked)]
        return sorted(matched, key=order)

    async def executemany(self, sql: str, records: list[tuple[Any, ...]]) -> None:
        """Store each inserted row after the others, in the order given."""
        assert "OVERRIDING SYSTEM VALUE" in sql
        columns = ("audit_id", "exception_id", "event", "trace_id", "recorded_at", "details")
        self.rows.extend(dict(zip(columns, record, strict=True)) for record in records)

    async def close(self) -> None:
        """Nothing to release."""


def _sample_rows() -> list[Row]:
    """Return every sample event as a stored row, numbered in load order."""
    samples = audit_history.load_sample_trails()
    return [
        {
            "audit_id": position + 1,
            "exception_id": event.exception_id,
            "event": event.event,
            "trace_id": event.trace_id,
            "recorded_at": event.recorded_at,
            "details": dict(event.details),
        }
        for position, event in sorted(samples.by_position.items())
    ]


def _stand_in(monkeypatch: pytest.MonkeyPatch, rule: str, rows: list[Row]) -> _Pool:
    """Point the audit store at one stand-in rule and the lab at a stand-in pool."""
    pool = _Pool(rows)

    async def create_pool(**_: object) -> _Pool:
        return pool

    monkeypatch.setattr(audit_store, "trail_for_exception", lambda asked: (rule, [asked]))
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    return pool


async def test_a_query_that_matches_without_regard_to_case_reads_both_ids_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supplied matching reads both ids identically."""
    baseline = audit_history.load_baseline()
    pool = _stand_in(monkeypatch, RULE_A, _sample_rows())

    comparison = await audit_lab.compare_trails("postgresql://stand-in/coldline")

    assert comparison.exceptions == len(baseline)
    assert len(comparison.differences) == 2 * len(baseline)
    assert not any(comparison.differences.values()), comparison.differences
    assert pool.asked == [
        asked for exception_id in baseline for asked in (exception_id, exception_id.upper())
    ]
    assert comparison.events_read == 2 * sum(len(trail) for trail in baseline.values())


async def test_a_query_that_drops_the_case_folding_is_reported_different(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching the id exactly returns nothing for the upper-case id: every one differs."""
    baseline = audit_history.load_baseline()
    _stand_in(monkeypatch, RULE_C, _sample_rows())

    comparison = await audit_lab.compare_trails("postgresql://stand-in/coldline")

    for exception_id, expected in baseline.items():
        assert comparison.differences[exception_id] == []
        upper = f"{exception_id} (asked as {exception_id.upper()})"
        assert comparison.differences[upper] == [f"0 events where the baseline has {len(expected)}"]


async def test_an_empty_table_reads_no_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no history loaded nothing is read, which the compare command turns into a hint."""
    _stand_in(monkeypatch, RULE_A, [])

    comparison = await audit_lab.compare_trails("postgresql://stand-in/coldline")

    assert comparison.events_read == 0
    assert all(comparison.differences.values())


def test_the_probe_trail_stores_same_second_events_against_audit_id_order() -> None:
    """Every tie group is inserted highest audit_id first; the trail is longer than any sample."""
    rows = audit_lab.probe_rows()
    longest_sample = max(len(trail) for trail in audit_history.load_baseline().values())

    assert len(rows) == audit_lab.PROBE_EVENTS > longest_sample
    assert len({row[0] for row in rows}) == len(rows)
    assert {row[1] for row in rows} == {audit_lab.PROBE_EXCEPTION_ID}
    assert [row[4] for row in rows] == sorted(row[4] for row in rows)
    ties = [row for row in rows if sum(other[4] == row[4] for other in rows) > 1]
    assert len(ties) == len(audit_lab.PROBE_TIE_STARTS) * audit_lab.PROBE_TIE_SIZE
    for first, second in zip(ties, ties[1:], strict=False):
        if first[4] == second[4]:
            assert first[0] > second[0]
    assert min(row[0] for row in rows) > audit_history.HISTORY_EVENTS


async def test_a_query_that_keeps_the_supplied_order_reads_the_probe_trail_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supplied matching and order read the probe trail exactly as the supplied query."""
    pool = _stand_in(monkeypatch, RULE_A, _sample_rows())

    differences = await audit_lab.probe_trails("postgresql://stand-in/coldline")

    probe = audit_lab.PROBE_EXCEPTION_ID
    assert differences == {f"probe {probe}": [], f"probe {probe} (asked as {probe.upper()})": []}
    assert pool.asked == [probe, probe.upper()]


async def test_a_query_that_orders_by_time_alone_is_reported_different(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-second probe events come back in stored order, against audit_id order: both differ."""
    _stand_in(monkeypatch, RULE_B, _sample_rows())

    differences = await audit_lab.probe_trails("postgresql://stand-in/coldline")

    assert len(differences) == 2
    for findings in differences.values():
        assert len(findings) == 1
        assert findings[0].startswith(f"event {audit_lab.PROBE_TIE_STARTS[0] + 1} differs in ")
        assert "audit_id" in findings[0]
