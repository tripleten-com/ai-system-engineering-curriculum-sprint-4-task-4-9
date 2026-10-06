"""Coldline.

===================

File:              tests/contract/test_authoring_contract.py
Component:         Repository integrity contract
Purpose:           Verifies required Task repository structure and configuration.
Interacts With:    Repository integrity checks and the complete Task tree
Sprint/Task:       Sprint 4 — Project 4
Concepts:          Dependency direction, configuration ownership, repository integrity
Tools:             Python 3.12, pytest
"""

import re
from pathlib import Path, PurePosixPath

import pytest

from tests.contract import authoring
from tests.contract.submission_validation import ALLOWED_PATHS, _changed_paths
from tests.security import repository

TASK_ROOT = Path(__file__).resolve().parents[2]
BANNER_PATTERN = re.compile(r"Coldline(?: — Task \d+\.\d+)?\.")
# Task 4.9's pull request changes at most three fixed files, and grants no directory prefix.
# Keep this in step with submission_validation.ALLOWED_PATHS and ALLOWED_PREFIXES. The audit
# store, the audit sink, the query lab and its fixtures, the existing migrations, the
# migration environment, the supplied tests and the workflows are absent on purpose: the
# lesson says they stay as supplied.
SUBMISSION_DIFF_ALLOWLIST = ALLOWED_PATHS
SUBMISSION_DIFF_PREFIXES: tuple[str, ...] = ()
# Besides the allowlist, the diff may add one new revision: one .py file directly in this
# directory that did not exist at the starting checkpoint. No revision that existed there
# may change or go. Keep this in step with submission_validation.validate_changed_paths.
MIGRATION_DIRECTORY = "migrations/versions"
MAX_NEW_MIGRATIONS = 1
HEADER_FIELDS = (
    "File:",
    "Component:",
    "Purpose:",
    "Interacts With:",
    "Sprint/Task:",
    "Concepts:",
    "Tools:",
)
# Supplied configuration only: the student-editable `submission.yaml` is not pinned here,
# since a non-assessed test must not hold a student to the starter's banner. The Task 4.6
# register is supplied in this Task, so its banner is pinned.
COMMENTABLE_CONFIGURATION = (
    ".devcontainer/post-create.sh",
    "alembic.ini",
    "config/auth.yaml",
    "config/retrieval-baseline.yaml",
    "config/student/retrieval.yaml",
    "docs/governance/risk-register.yaml",
    "docs/security/threat-catalog.yaml",
    "tests/fixtures/credentials/test-values.yaml",
    "tests/fixtures/pii/notes.yaml",
    "tests/fixtures/tokens/fixtures.yaml",
    "infra/profiles/object-store-fidelity.yaml",
    "infra/profiles/vector-engines.yaml",
    "infra/release/manifest.yaml",
    ".devcontainer/start-stack.sh",
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    ".github/workflows/security.yml",
    ".github/workflows/task.yml",
    ".gitignore",
    ".gitleaks.toml",
    ".semgrepignore",
    "compose.yaml",
    "infra/containers/api.Dockerfile",
    "infra/containers/worker.Dockerfile",
    "infra/observability/alertmanager.yml",
    "infra/observability/alerts.yml",
    "infra/observability/grafana/dashboards/provider.yml",
    "infra/observability/grafana/datasources/datasources.yml",
    "infra/observability/prometheus.yml",
    "infra/postgres/001_opening_checkpoint.sql",
    "infra/postgres/002_retrieval_corpus.sql",
    "infra/postgres/003_idempotency.sql",
    "infra/postgres/004_migration_baseline.sql",
    "infra/scripts/bootstrap.ps1",
    "infra/scripts/bootstrap.sh",
    "infra/scripts/preflight.ps1",
    "infra/scripts/preflight.sh",
    "pyproject.toml",
    "security/gate.yaml",
    "security/scanners.yaml",
    "security/semgrep-rules.yaml",
    "submission-sample.yaml",
)


def test_current_repository_satisfies_the_integrity_contract() -> None:
    """Fail when a protected repository invariant drifts."""
    assert authoring.main() == 0


@pytest.mark.parametrize(
    "fixture",
    [
        "infra/corpus/README.md",
        "infra/judge/README.md",
        "infra/profiles/README.md",
        "infra/audit/README.md",
    ],
)
def test_supplied_fixtures_carry_a_provenance_and_licence_record(fixture: str) -> None:
    """Every supplied fixture must say where it came from and under what terms."""
    record = (TASK_ROOT / fixture).read_text(encoding="utf-8")
    for required in ("Synthetic", "Licence", "Provenance"):
        assert required in record, f"{fixture} does not state {required.lower()}"


def test_python_files_have_the_student_navigation_banner() -> None:
    """Catch a supplied source file that gives students no ownership or purpose context.

    The student-editable query module is left out: a non-assessed test must not hold a
    student to the starter's banner.
    """
    failures: list[str] = []
    roots = [TASK_ROOT / "src", TASK_ROOT / "tests", TASK_ROOT / "infra/scripts"]
    for root in roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.relative_to(TASK_ROOT).as_posix() in ALLOWED_PATHS:
                continue
            text = path.read_text(encoding="utf-8")
            header = text[:1200]
            missing = [field for field in HEADER_FIELDS if field not in header]
            if not BANNER_PATTERN.search(header):
                missing.insert(0, "Coldline navigation banner")
            if missing:
                failures.append(f"{path.relative_to(TASK_ROOT)}: {', '.join(missing)}")

    assert failures == []


# Scoped to a student submission: it asserts the diff from the merge base stays inside this
# Task's student-editable boundary. A generated export PR necessarily changes more than that,
# so template CI deselects this marker. `poe contract` and `poe author-verify` run it; student
# CI relies on `poe submission`, which checks the same boundary.
@pytest.mark.submission_boundary
def test_submission_change_stays_within_the_permitted_diff() -> None:
    """Reject any change outside the three Task 4.9 student files and one new migration.

    The diff from the merge base may change `src/common/audit_queries.py`,
    `docs/student/audit-query-record.md` and `submission.yaml`, and may add at most one
    `.py` file directly in `migrations/versions/` that the starting checkpoint did not
    have. A change to, or the removal of, any migration the checkpoint had, a second new
    migration, a file nested deeper, or a change to the audit store, the audit sink, the
    query lab, its fixtures, the migration environment, a supplied test or a workflow fails
    it, whatever else that change does.
    """
    changed = [
        path for path in _changed_paths(TASK_ROOT) if not path.startswith(SUBMISSION_DIFF_PREFIXES)
    ]
    migrations = sorted(path for path in changed if path.startswith(f"{MIGRATION_DIRECTORY}/"))
    existing = [
        path for path in migrations if repository.baseline_text(TASK_ROOT, path) is not None
    ]
    misplaced = [
        path
        for path in migrations
        if PurePosixPath(path).parent.as_posix() != MIGRATION_DIRECTORY
        or PurePosixPath(path).suffix != ".py"
    ]

    assert existing == [], "an existing migration changed or was removed"
    assert misplaced == [], "a new migration is one .py file directly in migrations/versions/"
    assert len(migrations) <= MAX_NEW_MIGRATIONS
    assert set(changed) - set(migrations) <= SUBMISSION_DIFF_ALLOWLIST


def test_commentable_configuration_files_explain_their_role() -> None:
    """Catch operational files that provide configuration without context."""
    banner = re.compile(r"(?m)^(?:#|--) Coldline(?: - Task \d+\.\d+)?$")
    missing = []
    for relative in COMMENTABLE_CONFIGURATION:
        text = (TASK_ROOT / relative).read_text(encoding="utf-8")
        if not banner.search(text[:1000]):
            missing.append(relative)

    assert missing == []


def test_released_repository_has_no_unresolved_template_tokens() -> None:
    """A student-facing README must not contain an unresolved placeholder."""
    assert authoring._check_unresolved_template_tokens([TASK_ROOT / "README.md"]) == []
