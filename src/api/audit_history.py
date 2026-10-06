"""Coldline.

===================

File:              src/api/audit_history.py
Component:         Audit query lab — the supplied large history
Purpose:           Generate the supplied large, synthetic audit history, and read the sample
                    trails it contains and their stored baseline.
Interacts With:    infra/audit/sample-trails.json, infra/audit/trail-baseline.json,
                    src/api/audit_lab.py, src/common/audit.py, the audit_events table
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Deterministic fixtures, synthetic data, a total order
Tools:             Python 3.12

The history is ``HISTORY_EVENTS`` audit events, one per position, in the order they are
loaded. Most positions hold filler: ``HISTORY_EXCEPTIONS`` synthetic exceptions, each with
the four worker events and eight summary reads, spread so that one exception's events lie
far apart in the table, as a real history's do. ``recorded_at`` moves on one second per
position. The positions ``infra/audit/sample-trails.json`` names hold the sample trails
instead: five exceptions written out by hand, the measurement exception among them, with a
redelivery, a late-arriving read and reads recorded in the same second.

Everything is a pure function of the position, so every load writes the same rows in the
same order, and the table's own sequence (``audit_id``) increases with the position.
``infra/audit/trail-baseline.json`` is the stored baseline: each sample trail as the
supplied query returns it, oldest first with ties broken by ``audit_id``. Every stored
exception id is lower case, as every id the API mints is.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from common.audit import AuditRecord

FIXTURE_DIRECTORY = Path(__file__).resolve().parents[2] / "infra/audit"
SAMPLE_TRAILS_PATH = FIXTURE_DIRECTORY / "sample-trails.json"
TRAIL_BASELINE_PATH = FIXTURE_DIRECTORY / "trail-baseline.json"
HISTORY_EVENTS = 300_000
HISTORY_EXCEPTIONS = 25_000
HISTORY_START = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)
# The exception whose trail every Task 4.9 measurement uses; README.md names it too.
MEASUREMENT_EXCEPTION_ID = "exc-5c2e8f14-7a3b-5d91-b6e0-4f8a2c9d1e37"
# The columns a load writes, in the order of every row tuple; audit_id is the table's own.
COPY_COLUMNS: tuple[str, ...] = ("exception_id", "event", "trace_id", "recorded_at", "details")
HANDLING_CLASSES = (
    "thermal_excursion",
    "seal_integrity",
    "equipment_failure",
    "documentation_hold",
    "handling_rule_breach",
)
NEXT_STEPS = (
    "operational_review",
    "hold_at_relay",
    "escalate_to_duty_coordinator",
    "continue_transit",
)
SITES = ("Fresno relay", "Modesto dock", "Stockton hub", "Merced cross-dock", "Visalia depot")
ACTIONS = (
    "hold it at the relay and confirm with the clinic before release.",
    "send it to operational review before it continues.",
    "escalate to the duty coordinator and move the load to a backup unit.",
    "continue transit and attach the driver's temperature log.",
)
REVIEW_MESSAGE = (
    "Automatic summary withheld: the model's answer did not pass the output check. "
    "A dispatcher must review this exception before anyone acts on it."
)

CopyRecord = tuple[str, str, str | None, datetime, str]


@dataclass(frozen=True)
class HistoryEvent:
    """One audit event of the supplied history, as a load writes it."""

    exception_id: str
    event: str
    trace_id: str | None
    recorded_at: datetime
    details: dict[str, object]

    def copy_record(self) -> CopyRecord:
        """Return the row tuple a load writes, in ``COPY_COLUMNS`` order."""
        return (
            self.exception_id,
            self.event,
            self.trace_id,
            self.recorded_at,
            json.dumps(self.details, sort_keys=True),
        )

    def comparable(self) -> dict[str, object]:
        """Return the fields a trail comparison reads, as plain values."""
        return {
            "event": self.event,
            "exception_id": self.exception_id,
            "trace_id": self.trace_id,
            "recorded_at": self.recorded_at.astimezone(UTC).isoformat(),
            "details": self.details,
        }


@dataclass(frozen=True)
class SampleTrails:
    """The hand-written sample trails and the positions their events occupy."""

    measurement_exception_id: str
    exception_ids: tuple[str, ...]
    by_position: dict[int, HistoryEvent]


def comparable_record(record: AuditRecord) -> dict[str, object]:
    """Return a stored record's compared fields, in the same shape as ``comparable``."""
    return {
        "event": record.event,
        "exception_id": record.exception_id,
        "trace_id": record.trace_id,
        "recorded_at": record.recorded_at.astimezone(UTC).isoformat(),
        "details": record.details,
    }


@cache
def history_exception_id(number: int) -> str:
    """Return the id of one filler exception: ``exc-`` and a lower-case UUID, as the API's."""
    return f"exc-{uuid5(NAMESPACE_URL, f'coldline:audit-history:{number}')}"


def _hex(text: str) -> str:
    """Return the SHA-256 hex digest of a text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@cache
def _worker_trace(number: int) -> str:
    """Return the trace id one filler exception's worker events share."""
    return _hex(f"coldline:audit-history:trace:{number}")[:32]


def _summary(number: int) -> str:
    """Return the stored summary of one filler exception that completed."""
    peak = 8.4 + (number % 30) / 10
    minutes = 15 + number % 90
    return (
        f"Shipment SHP-{number:05d} held at {peak:.1f} C for {minutes} minutes against an "
        f"8.0 C limit at the {SITES[number % len(SITES)]}; {ACTIONS[number % len(ACTIONS)]}"
    )


def filler_event(position: int) -> HistoryEvent:
    """Return the filler event at one position of the history."""
    number = position % HISTORY_EXCEPTIONS
    step = position // HISTORY_EXCEPTIONS
    rejected = number % 9 == 0
    trace_id = _worker_trace(number)
    details: dict[str, object]
    if step == 0:
        event = "processing_requested"
        details = {"reading_id": f"reading-history-{number:05d}", "delivery_count": 1}
    elif step == 1:
        event = "model_responded"
        details = {
            "provider": "deterministic",
            "answer_digest": _hex(f"coldline:audit-history:answer:{number}"),
            "answer_length": 240 + number % 160,
        }
    elif step == 2 and rejected:
        event = "output_rejected"
        details = {"reason_code": "unknown_property"}
    elif step == 2:
        event = "output_validated"
        details = {
            "handling_class": HANDLING_CLASSES[number % len(HANDLING_CLASSES)],
            "next_step": NEXT_STEPS[number % len(NEXT_STEPS)],
        }
    elif step == 3:
        event = "outcome_stored"
        details = {
            "state": "NEEDS_REVIEW" if rejected else "COMPLETED",
            "summary": REVIEW_MESSAGE if rejected else _summary(number),
        }
    else:
        event = "summary_read"
        trace_id = _hex(f"coldline:audit-history:read:{number}:{step}")[:32]
        details = {
            "subject": f"user:dispatcher-{(number + step) % 40:02d}",
            "role": "dispatcher",
        }
    return HistoryEvent(
        exception_id=history_exception_id(number),
        event=event,
        trace_id=trace_id,
        recorded_at=HISTORY_START + timedelta(seconds=position),
        details=details,
    )


def _event(entry: Mapping[str, Any], exception_id: str) -> tuple[int, HistoryEvent]:
    """Return one sample event and its position, read from the fixture."""
    position = entry["position"]
    details = entry["details"]
    if isinstance(position, bool) or not isinstance(position, int):
        raise ValueError(f"sample event of {exception_id} has no integer position")
    if not 0 <= position < HISTORY_EVENTS:
        raise ValueError(f"sample position {position} is outside the history")
    if not isinstance(details, dict):
        raise ValueError(f"sample event at {position} has no details object")
    trace_id = entry.get("trace_id")
    return position, HistoryEvent(
        exception_id=exception_id,
        event=str(entry["event"]),
        trace_id=None if trace_id is None else str(trace_id),
        recorded_at=datetime.fromisoformat(str(entry["recorded_at"])).astimezone(UTC),
        details=dict(details),
    )


def load_sample_trails(path: Path = SAMPLE_TRAILS_PATH) -> SampleTrails:
    """Return the sample trails, refusing a fixture that breaks the history's own rules."""
    document = json.loads(path.read_text(encoding="utf-8"))
    by_position: dict[int, HistoryEvent] = {}
    exception_ids: list[str] = []
    for exception in document["exceptions"]:
        exception_id = str(exception["exception_id"])
        if exception_id != exception_id.lower():
            raise ValueError(f"sample exception id {exception_id} is not lower case")
        exception_ids.append(exception_id)
        for entry in exception["events"]:
            position, event = _event(entry, exception_id)
            if position in by_position:
                raise ValueError(f"two sample events share position {position}")
            by_position[position] = event
    measurement = str(document["measurement_exception_id"])
    if measurement not in exception_ids:
        raise ValueError("the measurement exception is not one of the sample trails")
    return SampleTrails(measurement, tuple(exception_ids), by_position)


def load_baseline(path: Path = TRAIL_BASELINE_PATH) -> dict[str, list[dict[str, object]]]:
    """Return the stored baseline: each sample trail as the supplied query reads it."""
    document = json.loads(path.read_text(encoding="utf-8"))
    trails = document["trails"]
    if not isinstance(trails, dict):
        raise ValueError("the stored baseline has no trails object")
    return {str(exception_id): list(rows) for exception_id, rows in trails.items()}


def history_events(samples: SampleTrails) -> Iterator[HistoryEvent]:
    """Yield the whole history in load order: the sample event at its position, else filler."""
    for position in range(HISTORY_EVENTS):
        sample = samples.by_position.get(position)
        yield sample if sample is not None else filler_event(position)


def expected_trails(samples: SampleTrails) -> dict[str, list[dict[str, object]]]:
    """Return each sample trail ordered as the supplied query orders it.

    Oldest ``recorded_at`` first, ties broken by ``audit_id``, which a load assigns in
    position order. The stored baseline must equal this; a unit test holds the two together.
    """
    trails: dict[str, list[tuple[datetime, int, HistoryEvent]]] = {
        exception_id: [] for exception_id in samples.exception_ids
    }
    for position, event in samples.by_position.items():
        trails[event.exception_id].append((event.recorded_at, position, event))
    ordered: dict[str, list[dict[str, object]]] = {}
    for exception_id, rows in trails.items():
        rows.sort(key=lambda row: (row[0], row[1]))
        ordered[exception_id] = [event.comparable() for _, _, event in rows]
    return ordered


def trail_differences(
    expected: Sequence[Mapping[str, object]], actual: Sequence[Mapping[str, object]]
) -> list[str]:
    """Return how a trail differs from its baseline: the count, then the first difference."""
    findings: list[str] = []
    if len(actual) != len(expected):
        findings.append(f"{len(actual)} events where the baseline has {len(expected)}")
    for position, (wanted, got) in enumerate(zip(expected, actual, strict=False), start=1):
        if dict(wanted) == dict(got):
            continue
        names = set(wanted) | set(got)
        fields = sorted(name for name in names if wanted.get(name) != got.get(name))
        findings.append(
            f"event {position} differs in {', '.join(fields)}: the baseline has "
            f"{wanted.get('event')} at {wanted.get('recorded_at')}, the query returned "
            f"{got.get('event')} at {got.get('recorded_at')}"
        )
        break
    return findings
