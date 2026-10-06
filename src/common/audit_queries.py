"""Coldline.

===================

File:              src/common/audit_queries.py
Component:         Common — Audit queries
Purpose:           Hold the SQL that reads one exception's audit trail, oldest first.
Interacts With:    src/adapters/persistence/audit_store.py (`poe audit-trail`),
                    src/api/audit_lab.py (`poe audit-explain`, `poe audit-benchmark`,
                    `poe audit-trail-compare`), the audit_events table
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Reconstructing one interaction, query plans, a total order
Tools:             Python 3.12, PostgreSQL

``trail_for_exception`` returns the reconstruction query and its parameters. The audit
store runs it for ``poe audit-trail``, and the Task 4.9 tools run the same query to plan
it, time it and compare what it returns. Whatever the SQL says, it must:

- select the six columns the store reads, under these names: ``audit_id``,
  ``exception_id``, ``event``, ``trace_id``, ``recorded_at`` and ``details``;
- return every record of the one exception, and no record of any other; and
- return them oldest first, with ties on ``recorded_at`` broken by ``audit_id``, the
  table's own sequence, so the order is total and no plan can reorder two records.

Operators paste exception ids from emails and tickets, sometimes in upper case, so the
supplied query matches the id without regard to letter case.

In Task 4.9 this module is one of your permitted files. If your one change is a rewrite,
change only the SQL below and keep the parameters as they are.
"""


def trail_for_exception(exception_id: str) -> tuple[str, list[object]]:
    """Return the SQL that reads one exception's audit trail, and its parameters, in order.

    The parameters reach the database as ``$1``, ``$2`` and so on; the exception id the
    operator asked for is ``$1``.
    """
    sql = """
        SELECT audit_id, exception_id, event, trace_id, recorded_at, details
        FROM audit_events
        WHERE lower(exception_id) = lower($1)
        ORDER BY recorded_at, audit_id
    """
    return sql, [exception_id]
