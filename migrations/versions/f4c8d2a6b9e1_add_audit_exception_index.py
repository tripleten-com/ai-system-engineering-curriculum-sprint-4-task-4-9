"""Coldline.

===================

File:              migrations/versions/ (revision f4c8d2a6b9e1)
Component:         Migrations — add the audit exception index
Purpose:           Index audit_events on exception_id.
Interacts With:    PostgreSQL, the audit_events table, src/adapters/persistence/audit_store.py
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Reversible schema evolution, secondary indexes
Tools:             Python 3.12, Alembic

Revision ID: f4c8d2a6b9e1
Revises: e5f2a8c4d6b1
Create Date: 2026-10-06

One B-tree index on `audit_events.exception_id`, named `ix_audit_events_exception_id`.
The initializer applies it to an empty table on the first start, so building it takes no
time there. This is the head of the supplied chain: a new revision revises it.

`downgrade` drops the index and nothing else.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f4c8d2a6b9e1"
down_revision: str | None = "e5f2a8c4d6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the index on exception_id."""
    op.create_index("ix_audit_events_exception_id", "audit_events", ["exception_id"])


def downgrade() -> None:
    """Reverse exactly what `upgrade` applied, and nothing more."""
    op.drop_index("ix_audit_events_exception_id", table_name="audit_events")
