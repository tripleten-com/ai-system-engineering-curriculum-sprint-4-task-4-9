"""Coldline.

===================

File:              tests/unit/adapters/test_audit_store.py
Component:         Unit tests — PostgreSQL audit store
Purpose:           Prove the store reads a trail with whatever SQL and parameters
                    `trail_for_exception` returns, and builds records from the six columns.
Interacts With:    src/adapters/persistence/audit_store.py, src/common/audit_queries.py
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          One query for every reader, boundary translation
Tools:             Python 3.12, pytest

No database: the pool is a stand-in that records what it was asked. The query module is
replaced by a stub, so nothing here depends on the SQL the query module holds; that SQL is
yours to change in Task 4.9.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from adapters.persistence import audit_store

RECORDED_AT = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)


class _Pool:
    """Stand in for an asyncpg pool: record each fetch and answer with fixed rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Hold the rows every fetch returns."""
        self.rows = rows
        self.fetches: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *arguments: object) -> list[dict[str, Any]]:
        """Record one query and return the fixed rows."""
        self.fetches.append((sql, arguments))
        return self.rows


def _row(audit_id: int, details: object) -> dict[str, Any]:
    """Return one row with the table's six columns."""
    return {
        "audit_id": audit_id,
        "exception_id": "exc-example",
        "event": "summary_read",
        "trace_id": "a" * 32,
        "recorded_at": RECORDED_AT,
        "details": details,
    }


async def test_the_trail_runs_the_query_module_s_sql_and_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever trail_for_exception returns is what the store sends, in order."""
    monkeypatch.setattr(
        audit_store,
        "trail_for_exception",
        lambda exception_id: ("SELECT stand_in($1, $2)", [exception_id, "second"]),
    )
    pool = _Pool([_row(7, json.dumps({"subject": "user:d", "role": "d"})), _row(8, {})])

    records = await audit_store.PostgresAuditStore(pool).trail("exc-example")

    assert pool.fetches == [("SELECT stand_in($1, $2)", ("exc-example", "second"))]
    assert [record.audit_id for record in records] == [7, 8]
    assert records[0].details == {"subject": "user:d", "role": "d"}
    assert records[1].details == {}


def test_a_row_becomes_the_sink_s_record() -> None:
    """The six columns map to the record's fields; a non-object details becomes empty."""
    record = audit_store.record_from_row(_row(3, "[1, 2]"))

    assert record.audit_id == 3
    assert record.exception_id == "exc-example"
    assert record.event == "summary_read"
    assert record.trace_id == "a" * 32
    assert record.recorded_at == RECORDED_AT
    assert record.details == {}
