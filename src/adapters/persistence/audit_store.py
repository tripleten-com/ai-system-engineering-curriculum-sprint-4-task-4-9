"""Coldline.

===================

File:              src/adapters/persistence/audit_store.py
Component:         Adapter — PostgreSQL audit store
Purpose:           Keep the audit records the sink builds in the audit_events table.
Interacts With:    src/common/audit.py, src/common/audit_queries.py, PostgreSQL,
                    migrations/versions/ (audit_events)
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Append-only evidence, total ordering, boundary translation
Tools:             Python 3.12, PostgreSQL, asyncpg

The table is deliberately plain: an append-only log keyed by a sequence number, with the
exception id and the trace id as columns and the event's fields as one JSON document.
Task 4.9 supplies one secondary index on it, ``ix_audit_events_exception_id`` on
``exception_id``, and moves the reconstruction query into
``common.audit_queries.trail_for_exception``: ``trail`` below runs whatever SQL that
function returns, so ``poe audit-trail`` and the Task 4.9 tools always read the same query.
"""

import json
from typing import Any

import asyncpg

from common.audit import AuditRecord
from common.audit_queries import trail_for_exception


class PostgresAuditStore:
    """Append audit records to PostgreSQL and read one exception's trail back in order."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind the store to one initialized connection pool."""
        self._pool = pool

    async def append(self, record: AuditRecord) -> AuditRecord:
        """Insert one record and return it with the sequence number the table assigned."""
        row = await self._pool.fetchrow(
            """
            INSERT INTO audit_events (exception_id, event, trace_id, recorded_at, details)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            RETURNING *
            """,
            record.exception_id,
            record.event,
            record.trace_id,
            record.recorded_at,
            json.dumps(record.details, sort_keys=True, default=str),
        )
        if row is None:
            raise RuntimeError("audit insert did not return the stored record")
        return record_from_row(row)

    async def trail(self, exception_id: str) -> list[AuditRecord]:
        """Return one exception's records as ``trail_for_exception`` reads them, oldest first."""
        sql, parameters = trail_for_exception(exception_id)
        rows = await self._pool.fetch(sql, *parameters)
        return [record_from_row(row) for row in rows]


def record_from_row(row: asyncpg.Record) -> AuditRecord:
    """Convert one database row with the table's six columns to the sink's record."""
    details_value: Any = row["details"]
    if isinstance(details_value, str):
        details_value = json.loads(details_value)
    details = details_value if isinstance(details_value, dict) else {}
    return AuditRecord(
        event=row["event"],
        exception_id=row["exception_id"],
        trace_id=row["trace_id"],
        recorded_at=row["recorded_at"],
        details=dict(details),
        audit_id=row["audit_id"],
    )
