"""Coldline.

===================

File:              tests/security/audit_verify.py
Component:         Security tooling — The slow-query check's evidence
Purpose:           Run `poe verify`'s fresh-database procedure inside the API container and read
                    its report, and classify the one change the diff from the starting checkpoint
                    holds: a new migration or a rewritten trail query.
Interacts With:    src/api/audit_lab.py (through `docker compose exec`), src/api/audit_plan.py,
                    tests/security/repository.py, tests/contract/test_audit_query_contract.py,
                    pyproject.toml (`poe audit-verify`)
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Evidence from a fresh database, one change with one explanation
Tools:             Python 3.12, Docker Compose, Git

``run_lab`` runs ``python -m api.audit_lab verify-run`` inside the ``api`` container and
returns the JSON report it prints; the procedure creates, uses and drops a database of its
own and never reads or writes the stack's. ``change_report`` reads the working tree against
the starting checkpoint (the merge base with ``main``): which files under
``migrations/versions/`` are new, which existing ones changed or went, and whether
``src/common/audit_queries.py`` changed. An uncommitted file counts: the image ``poe
verify`` builds carries the working tree.

``poe audit-verify`` prints both on their own, as a diagnosis; ``poe verify`` reads them
through the assessed module.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.audit_plan import REPORT_MARKER
from tests.security import repository
from tests.security.live_record import COMPOSE

TASK_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIRECTORY = "migrations/versions/"
QUERY_MODULE = "src/common/audit_queries.py"
LAB_TIMEOUT_SECONDS = 1800
OUTPUT_TAIL_CHARACTERS = 3000


class AuditVerifyError(RuntimeError):
    """Report that the procedure could not run or printed no report, as opposed to a finding."""


@dataclass(frozen=True)
class ChangeReport:
    """The one change a Task 4.9 diff holds, as the working tree shows it.

    ``history`` is False when the Task root has no student history to diff (the curriculum's
    authoring snapshot), and every other field is then empty.
    """

    history: bool
    added_migrations: tuple[str, ...]
    changed_migrations: tuple[str, ...]
    query_changed: bool

    @property
    def change_type(self) -> str | None:
        """Return ``index`` or ``rewrite`` when the diff holds exactly one change, else None."""
        return classify(self.added_migrations, self.changed_migrations, self.query_changed)

    def describe(self) -> str:
        """Return the diff's changes in one sentence, for a finding."""
        added = (
            f"{len(self.added_migrations)} new file(s) in {MIGRATION_DIRECTORY} "
            f"({', '.join(self.added_migrations)})"
            if self.added_migrations
            else f"no new file in {MIGRATION_DIRECTORY}"
        )
        changed = (
            f", changed or removed existing migration(s) {', '.join(self.changed_migrations)}"
            if self.changed_migrations
            else ""
        )
        query = "a change" if self.query_changed else "no change"
        return f"{added}{changed}, and {query} to {QUERY_MODULE}"


def classify(added: tuple[str, ...], changed: tuple[str, ...], query_changed: bool) -> str | None:
    """Return the change type a diff's parts make, or None unless they are exactly one change."""
    if changed:
        return None
    if len(added) == 1 and not query_changed:
        return "index"
    if not added and query_changed:
        return "rewrite"
    return None


def _normalized(text: str) -> str:
    """Return text with CRLF line endings folded to LF, as Git stores it."""
    return text.replace("\r\n", "\n")


def _current_text(path: Path) -> str | None:
    """Return a working-tree file's text, or None when it is absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def change_report(root: Path = TASK_ROOT) -> ChangeReport:
    """Return what the working tree changed since the starting checkpoint, for this Task."""
    if not repository.is_student_checkout(root):
        return ChangeReport(False, (), (), False)
    listed = repository.listed_files(root) or []
    candidates = {path for path in listed if path.startswith(MIGRATION_DIRECTORY)}
    candidates.update(
        path for path in repository.changed_paths(root) if path.startswith(MIGRATION_DIRECTORY)
    )
    added: list[str] = []
    changed: list[str] = []
    for path in sorted(candidates):
        if "__pycache__" in path.split("/") or path.endswith(".pyc"):
            continue
        baseline = repository.baseline_text(root, path)
        current = _current_text(root / path)
        if baseline is None:
            if current is not None:
                added.append(path)
        elif current is None or _normalized(current) != _normalized(baseline):
            changed.append(path)
    query_baseline = repository.baseline_text(root, QUERY_MODULE)
    query_current = _current_text(root / QUERY_MODULE)
    query_changed = (
        query_baseline is None
        or query_current is None
        or _normalized(query_current) != _normalized(query_baseline)
    )
    return ChangeReport(True, tuple(added), tuple(changed), query_changed)


def parse_report(stdout: str) -> dict[str, Any] | None:
    """Return the JSON report from the procedure's output, or None when it printed none."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(REPORT_MARKER):
            try:
                loaded = json.loads(line[len(REPORT_MARKER) :])
            except ValueError:
                return None
            return loaded if isinstance(loaded, dict) else None
    return None


def run_lab(root: Path = TASK_ROOT) -> dict[str, Any]:
    """Run the fresh-database procedure inside the running API container and return its report."""
    command = [*COMPOSE, "exec", "-T", "api", "python", "-m", "api.audit_lab", "verify-run"]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=LAB_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditVerifyError(f"the verification procedure could not run: {exc}") from exc
    report = parse_report(completed.stdout)
    if report is None:
        tail = (completed.stdout + completed.stderr)[-OUTPUT_TAIL_CHARACTERS:]
        raise AuditVerifyError(
            f"the verification procedure printed no report (exit {completed.returncode}); is the "
            f"stack running (`poe start`) and the API image current (`poe rebuild-api`)?\n{tail}"
        )
    return report


def _reads(plan: object) -> str:
    """Return a plan observation's audit-table reads as one line."""
    if not isinstance(plan, dict):
        return "not observed"
    reads = plan.get("reads") or []
    parts: list[str] = []
    for read in reads:
        index = read.get("index")
        via = f" through {index}" if index else " without an index"
        if index and not read.get("index_cond"):
            via += " (no index condition)"
        sort = ", a sort above it" if read.get("sorted_above") else ""
        parts.append(f"{read.get('node')}{via}{sort}")
    return "; ".join(parts) or "no node reads the audit table"


def summary_lines(change: ChangeReport, report: dict[str, Any]) -> list[str]:
    """Return what `poe audit-verify` prints: the diff, the steps, the plans, the trails."""
    kind = change.change_type or "not exactly one change"
    lines = [f"diff: {change.describe()}", f"change type: {kind}"]
    if report.get("fatal"):
        lines.append(f"FATAL: {report['fatal']}")
    for name, step in (report.get("steps") or {}).items():
        state = "skipped" if step.get("skipped") else ("ok" if step.get("ok") else "FAILED")
        lines.append(f"step {name}: {state}")
    for name, error in (report.get("errors") or {}).items():
        lines.append(f"error in {name}: {error}")
    lines.append(f"plan before (supplied query): {_reads(report.get('plan_before'))}")
    lines.append(f"plan after (your query): {_reads(report.get('plan_after'))}")
    for label in ("trails_before", "trails_after"):
        trails = report.get(label)
        if isinstance(trails, dict):
            different = sorted(name for name, findings in trails.items() if findings)
            verdict = f"differ for {', '.join(different)}" if different else "identical"
            lines.append(f"{label.replace('_', ' ')}: {verdict}")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Print the diff's change and the fresh-database procedure's report on their own."""
    parser = argparse.ArgumentParser(description="Diagnose the Task 4.9 check on its own.")
    parser.add_argument("--root", type=Path, default=TASK_ROOT)
    arguments = parser.parse_args(argv)
    change = change_report(arguments.root)
    try:
        report = run_lab(arguments.root)
    except AuditVerifyError as exc:
        print(f"audit-verify: {exc}", file=sys.stderr)
        return 2
    print("\n".join(summary_lines(change, report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
