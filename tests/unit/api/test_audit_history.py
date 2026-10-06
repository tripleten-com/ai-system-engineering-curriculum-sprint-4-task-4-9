"""Coldline.

===================

File:              tests/unit/api/test_audit_history.py
Component:         Unit tests — The supplied large audit history
Purpose:           Hold the stored baseline to the order the supplied query gives the samples,
                    and prove the generated history is deterministic, lower case throughout, and
                    places each sample event at its position.
Interacts With:    src/api/audit_history.py, infra/audit/sample-trails.json,
                    infra/audit/trail-baseline.json, README.md
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Deterministic fixtures, a total order, synthetic data
Tools:             Python 3.12, pytest
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api import audit_history
from common.audit import AuditRecord

TASK_ROOT = Path(__file__).resolve().parents[3]
_API_IDENTITY = re.compile(r"^exc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
COMPARED_FIELDS = {"event", "exception_id", "trace_id", "recorded_at", "details"}


def test_the_stored_baseline_is_the_supplied_order_of_the_sample_trails() -> None:
    """Oldest recorded_at first, ties by load position: the stored baseline, exactly."""
    samples = audit_history.load_sample_trails()

    assert audit_history.load_baseline() == audit_history.expected_trails(samples)


def test_the_baseline_rows_carry_the_compared_fields_and_nothing_else() -> None:
    """Every baseline event has the five fields the comparison reads."""
    for exception_id, rows in audit_history.load_baseline().items():
        assert rows, exception_id
        for row in rows:
            assert set(row) == COMPARED_FIELDS
            assert row["exception_id"] == exception_id


def test_the_measurement_exception_is_a_sample_and_named_in_the_readme() -> None:
    """The fixture, the module and README.md name the same measurement exception."""
    samples = audit_history.load_sample_trails()

    assert samples.measurement_exception_id == audit_history.MEASUREMENT_EXCEPTION_ID
    assert samples.measurement_exception_id in samples.exception_ids
    readme = (TASK_ROOT / "README.md").read_text(encoding="utf-8")
    assert audit_history.MEASUREMENT_EXCEPTION_ID in readme


def test_every_sample_exception_id_has_the_api_shape() -> None:
    """Every stored id is exc- and a lower-case UUID, as the API mints them."""
    samples = audit_history.load_sample_trails()

    assert all(_API_IDENTITY.match(exception_id) for exception_id in samples.exception_ids)
    assert all(_API_IDENTITY.match(event.exception_id) for event in samples.by_position.values())


def test_one_sample_trail_orders_differently_from_its_load_order() -> None:
    """At least one trail's time order differs from its position order, so order is tested."""
    samples = audit_history.load_sample_trails()
    expected = audit_history.expected_trails(samples)
    differs = False
    for exception_id in samples.exception_ids:
        by_position = [
            event.comparable()
            for _, event in sorted(samples.by_position.items())
            if event.exception_id == exception_id
        ]
        differs = differs or by_position != expected[exception_id]
    assert differs


def test_a_fixture_that_breaks_the_rules_is_refused(tmp_path: Path) -> None:
    """Two events at one position, an upper-case id, or a position outside the history."""
    document = json.loads(audit_history.SAMPLE_TRAILS_PATH.read_text(encoding="utf-8"))
    first = document["exceptions"][0]

    shared = json.loads(json.dumps(document))
    shared["exceptions"][1]["events"][0]["position"] = first["events"][0]["position"]
    path = tmp_path / "shared.json"
    path.write_text(json.dumps(shared), encoding="utf-8")
    with pytest.raises(ValueError, match="share position"):
        audit_history.load_sample_trails(path)

    upper = json.loads(json.dumps(document))
    upper["exceptions"][0]["exception_id"] = first["exception_id"].upper()
    path = tmp_path / "upper.json"
    path.write_text(json.dumps(upper), encoding="utf-8")
    with pytest.raises(ValueError, match="not lower case"):
        audit_history.load_sample_trails(path)

    outside = json.loads(json.dumps(document))
    outside["exceptions"][0]["events"][0]["position"] = audit_history.HISTORY_EVENTS
    path = tmp_path / "outside.json"
    path.write_text(json.dumps(outside), encoding="utf-8")
    with pytest.raises(ValueError, match="outside the history"):
        audit_history.load_sample_trails(path)


def test_the_filler_is_deterministic_and_lower_case() -> None:
    """The same position always gives the same event, and every filler id has the API shape."""
    for position in (0, 1, 24_999, 25_000, 150_000, audit_history.HISTORY_EVENTS - 1):
        event = audit_history.filler_event(position)
        assert event == audit_history.filler_event(position)
        assert _API_IDENTITY.match(event.exception_id)
        assert event.copy_record()[4] == json.dumps(event.details, sort_keys=True)


def test_one_filler_exception_is_one_interaction_in_time_order() -> None:
    """Its twelve events are the worker's four, then summary reads, oldest first."""
    number = 1_234
    steps = audit_history.HISTORY_EVENTS // audit_history.HISTORY_EXCEPTIONS
    events = [
        audit_history.filler_event(step * audit_history.HISTORY_EXCEPTIONS + number)
        for step in range(steps)
    ]

    assert {event.exception_id for event in events} == {audit_history.history_exception_id(number)}
    assert [event.event for event in events[:4]] == [
        "processing_requested",
        "model_responded",
        "output_validated",
        "outcome_stored",
    ]
    assert {event.event for event in events[4:]} == {"summary_read"}
    times = [event.recorded_at for event in events]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert len({event.trace_id for event in events[:4]}) == 1


def test_the_history_places_each_sample_event_at_its_position() -> None:
    """The generator yields the sample event where the fixture puts it, filler elsewhere."""
    samples = audit_history.load_sample_trails()
    first_sample = min(samples.by_position)
    head = list(itertools.islice(audit_history.history_events(samples), first_sample + 2))

    assert head[first_sample] == samples.by_position[first_sample]
    assert head[0] == audit_history.filler_event(0)
    assert head[first_sample + 1] == samples.by_position.get(
        first_sample + 1, audit_history.filler_event(first_sample + 1)
    )


def test_trail_differences_report_the_count_and_the_first_difference() -> None:
    """Identical trails give nothing; a reorder names its first position; a short one its count."""
    rows = audit_history.load_baseline()[audit_history.MEASUREMENT_EXCEPTION_ID]

    assert audit_history.trail_differences(rows, list(rows)) == []
    swapped = [rows[1], rows[0], *rows[2:]]
    findings = audit_history.trail_differences(rows, swapped)
    assert len(findings) == 1 and findings[0].startswith("event 1 differs in ")
    short = audit_history.trail_differences(rows, rows[:-1])
    assert short == [f"{len(rows) - 1} events where the baseline has {len(rows)}"]


def test_a_stored_record_compares_like_its_fixture_event() -> None:
    """A record read back from the store has the same compared fields as the event loaded."""
    event = audit_history.filler_event(42)
    record = AuditRecord(
        event=event.event,
        exception_id=event.exception_id,
        trace_id=event.trace_id,
        recorded_at=event.recorded_at.astimezone(UTC),
        details=dict(event.details),
        audit_id=43,
    )

    assert audit_history.comparable_record(record) == event.comparable()
    expected_time = datetime(2026, 8, 3, 6, 0, 42, tzinfo=UTC).isoformat()
    assert event.comparable()["recorded_at"] == expected_time
