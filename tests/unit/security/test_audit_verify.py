"""Coldline.

===================

File:              tests/unit/security/test_audit_verify.py
Component:         Unit tests — The slow-query check's evidence
Purpose:           Prove the diff is classified as one index change, one rewrite, or neither, that
                    the report line is found in the procedure's output, and that the procedure
                    runs inside the API container without touching a host port.
Interacts With:    tests/security/audit_verify.py, src/api/audit_plan.py
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          One change with one explanation, evidence from a fresh database
Tools:             Python 3.12, pytest

No Docker and no Git: the command runner is replaced, and the classification is tested on
its inputs. The revision file names below are invented.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from api.audit_plan import REPORT_MARKER
from tests.security import audit_verify
from tests.security.audit_verify import ChangeReport, classify

NEW = ("migrations/versions/0f1e2d3c4b5a_example_revision.py",)
EXISTING = ("migrations/versions/e5f2a8c4d6b1_add_audit_events.py",)


@pytest.mark.parametrize(
    "added,changed,query_changed,expected",
    [
        (NEW, (), False, "index"),
        ((), (), True, "rewrite"),
        ((), (), False, None),
        (NEW, (), True, None),
        ((*NEW, "migrations/versions/1a2b3c4d5e6f_second.py"), (), False, None),
        (NEW, EXISTING, False, None),
        ((), EXISTING, True, None),
    ],
    ids=[
        "index",
        "rewrite",
        "nothing",
        "both",
        "two-migrations",
        "edited-existing",
        "edit-and-rewrite",
    ],
)
def test_the_diff_is_one_change_or_none(
    added: tuple[str, ...], changed: tuple[str, ...], query_changed: bool, expected: str | None
) -> None:
    """Exactly one new migration, or exactly the query, and no existing migration touched."""
    assert classify(added, changed, query_changed) == expected
    report = ChangeReport(True, added, changed, query_changed)
    assert report.change_type == expected
    assert "src/common/audit_queries.py" in report.describe()


def test_the_description_names_what_the_diff_holds() -> None:
    """The finding text names the new files, the changed ones, and the query's state."""
    text = ChangeReport(True, NEW, EXISTING, True).describe()

    assert NEW[0] in text and EXISTING[0] in text and "a change to" in text
    assert "no new file" in ChangeReport(True, (), (), False).describe()


def test_a_root_without_student_history_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The authoring snapshot has no diff to classify; nothing is read from Git."""
    monkeypatch.setattr(audit_verify.repository, "is_student_checkout", lambda root: False)

    report = audit_verify.change_report(tmp_path)

    assert report == ChangeReport(False, (), (), False)
    assert report.change_type is None


def test_the_report_line_is_found_after_any_other_output() -> None:
    """The last marked line is the report; anything else printed around it is ignored."""
    report = {"fatal": None, "steps": {}}
    stdout = f"INFO starting\n{REPORT_MARKER}{json.dumps(report)}\n"

    assert audit_verify.parse_report(stdout) == report
    assert audit_verify.parse_report("no report here\n") is None
    assert audit_verify.parse_report(f"{REPORT_MARKER}not json\n") is None
    assert audit_verify.parse_report(f"{REPORT_MARKER}[1, 2]\n") is None


class _Runner:
    """Stand in for subprocess.run, recording the command and answering with fixed output."""

    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        """Hold the output every call answers with."""
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        """Record one command and answer it."""
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


def test_the_procedure_runs_inside_the_api_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """`docker compose exec -T api python -m api.audit_lab verify-run`, and its report back."""
    runner = _Runner(f"{REPORT_MARKER}{json.dumps({'fatal': None})}\n")
    monkeypatch.setattr(audit_verify.subprocess, "run", runner)

    assert audit_verify.run_lab(Path(".")) == {"fatal": None}
    command = runner.commands[0]
    assert command[: len(audit_verify.COMPOSE)] == list(audit_verify.COMPOSE)
    assert command[len(audit_verify.COMPOSE) :] == [
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "api.audit_lab",
        "verify-run",
    ]


def test_no_report_is_a_tooling_error_with_the_output_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A procedure that printed no report raises, quoting what it printed."""
    runner = _Runner("", returncode=1, stderr="service api is not running")
    monkeypatch.setattr(audit_verify.subprocess, "run", runner)

    with pytest.raises(audit_verify.AuditVerifyError, match="service api is not running"):
        audit_verify.run_lab(Path("."))


def test_the_summary_names_the_steps_the_reads_and_the_trails() -> None:
    """`poe audit-verify` prints the diff, each step, both plans and both trail verdicts."""
    report: dict[str, Any] = {
        "fatal": None,
        "steps": {"upgrade": {"ok": True, "skipped": False}, "downgrade": {"ok": False}},
        "errors": {},
        "plan_before": {"reads": [{"node": "Example Read", "index": None, "sorted_above": True}]},
        "plan_after": {
            "reads": [
                {"node": "Other Read", "index": "ix_x", "index_cond": True, "sorted_above": False},
                {"node": "Third Read", "index": "ix_y", "index_cond": False, "sorted_above": False},
            ]
        },
        "trails_before": {"exc-a": []},
        "trails_after": {"exc-a": ["event 1 differs in event"]},
    }

    lines = audit_verify.summary_lines(ChangeReport(True, NEW, (), False), report)

    assert "change type: index" in lines
    assert "step upgrade: ok" in lines and "step downgrade: FAILED" in lines
    assert "plan before (supplied query): Example Read without an index, a sort above it" in lines
    assert (
        "plan after (your query): Other Read through ix_x; "
        "Third Read through ix_y (no index condition)"
    ) in lines
    assert "trails before: identical" in lines and "trails after: differ for exc-a" in lines
