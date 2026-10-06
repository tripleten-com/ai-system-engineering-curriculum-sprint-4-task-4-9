"""Coldline.

===================

File:              tests/unit/security/test_integrity.py
Component:         Unit tests — Verification integrity snapshot
Purpose:           Prove the snapshot names every file changed, added, or removed while `poe
                    verify` ran, covers the answer sheet and the student files first and the
                    trusted material after them, and skips the caches and the files the run
                    itself writes.
Interacts With:    tests/security/integrity.py, pyproject.toml
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Trusted bookends, content hashes, tamper evidence
Tools:             Python 3.12, pytest

The tests build a small tree shaped like the Task's trusted paths under ``tmp_path`` and
point the snapshot at a file beside it, so nothing here touches the real checkout or the
system temporary directory. The student files keep their real names, because the trusted
paths name them; every other file is invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.security import integrity

TRUSTED_TREE = {
    "pyproject.toml": "[tool.poe.tasks]\nverify = []\n",
    "uv.lock": "version = 1\n",
    "submission.yaml": "answers: {}\n",
    "src/common/audit_queries.py": "QUERY = ()\n",
    "docs/student/audit-query-record.md": "# notes\n",
    "migrations/versions/0001_example.py": "revision = 'example'\n",
    "migrations/env.py": "ENVIRONMENT = ()\n",
    "docs/governance/example-record.md": "# record\n",
    "docs/security/example-policy.md": "# policy\n",
    "docs/student/example-contract.md": "# contract\n",
    # Invented application modules; nothing here names a file of the shipped tree.
    "src/example/module.py": "def example_function():\n    return None\n",
    "src/example/other.py": "EXAMPLES = ()\n",
    "security/gate.yaml": "secrets: any\n",
    "security/scanners.yaml": "scanners: {}\n",
    ".gitleaks.toml": "[extend]\nuseDefault = true\n",
    ".semgrepignore": ".git/\n",
    ".github/workflows/task.yml": "name: Task verification\n",
    ".github/workflows/security.yml": "name: Security gate\n",
    "tests/contract/test_example_contract.py": "def test_row():\n    pass\n",
    "tests/security/example.py": "EXAMPLE = ()\n",
    "tests/student/test_example.py": "def test_student():\n    pass\n",
    "tests/fixtures/example/values.yaml": "fixtures: {}\n",
    "schemas/exception-summary.schema.json": "{}\n",
    "docs/contracts/submission.schema.json": "{}\n",
    "config/auth.yaml": "auth: {}\n",
    "infra/corpus/documents.jsonl": "{}\n",
    "infra/audit/example.json": "{}\n",
    "infra/postgres/example.sql": "SELECT 1;\n",
    "alembic.ini": "[alembic]\n",
}
EXPECTED_ORDER = [
    "submission.yaml",
    "src/common/audit_queries.py",
    "docs/student/audit-query-record.md",
    "migrations/versions/0001_example.py",
    "docs/governance/example-record.md",
    "docs/security/example-policy.md",
    "docs/student/example-contract.md",
    "src/example/module.py",
    "src/example/other.py",
    "security/gate.yaml",
    "security/scanners.yaml",
    ".gitleaks.toml",
    ".semgrepignore",
    ".github/workflows/task.yml",
    ".github/workflows/security.yml",
    "tests/contract/test_example_contract.py",
    "tests/security/example.py",
    "tests/student/test_example.py",
    "tests/fixtures/example/values.yaml",
    "schemas/exception-summary.schema.json",
    "docs/contracts/submission.schema.json",
    "config/auth.yaml",
    "infra/corpus/documents.jsonl",
    "infra/audit/example.json",
    "infra/postgres/example.sql",
    "migrations/env.py",
    "alembic.ini",
    "pyproject.toml",
    "uv.lock",
]


def _tree(root: Path) -> None:
    """Write the small trusted tree, plus noise the snapshot must ignore."""
    for relative, text in TRUSTED_TREE.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    cache = root / "tests/security/__pycache__"
    cache.mkdir()
    (cache / "example.cpython-312.pyc").write_bytes(b"\x00")
    migration_cache = root / "migrations/versions/__pycache__"
    migration_cache.mkdir()
    (migration_cache / "0001_example.cpython-312.pyc").write_bytes(b"\x00")
    (root / "README.md").write_text("not covered\n", encoding="utf-8")
    (root / "reports/security").mkdir(parents=True)
    (root / "reports/security/scan.sarif").write_text("{}\n", encoding="utf-8")
    (root / "evidence").mkdir()
    (root / "evidence/attack-dev.json").write_text("{}\n", encoding="utf-8")


def test_the_snapshot_covers_the_trusted_paths_and_skips_caches_and_run_output(
    tmp_path: Path,
) -> None:
    """Every trusted file is hashed; caches, the README, the reports and the evidence are not."""
    _tree(tmp_path)

    files = [path.as_posix() for path in integrity.trusted_files(tmp_path)]

    assert files == EXPECTED_ORDER
    digests = integrity.snapshot(tmp_path)
    assert set(digests) == set(files)
    assert all(len(digest) == 64 for digest in digests.values())


def test_the_trusted_paths_name_the_answer_sheet_and_the_student_files_first() -> None:
    """The snapshot starts with the answer sheet and the student files, then the rest."""
    assert integrity.TRUSTED_PATHS[:4] == (
        "submission.yaml",
        "src/common/audit_queries.py",
        "docs/student/audit-query-record.md",
        "migrations/versions/",
    )
    for trusted in (
        "docs/governance/",
        "docs/security/",
        "docs/student/",
        "src/",
        "security/",
        ".gitleaks.toml",
        ".semgrepignore",
        ".github/workflows/task.yml",
        ".github/workflows/security.yml",
        "tests/contract/",
        "tests/security/",
        "tests/student/",
        "tests/fixtures/",
        "schemas/",
        "docs/contracts/submission.schema.json",
        "config/auth.yaml",
        "infra/corpus/documents.jsonl",
        "infra/audit/",
        "infra/postgres/",
        "migrations/",
        "alembic.ini",
        "pyproject.toml",
        "uv.lock",
    ):
        assert trusted in integrity.TRUSTED_PATHS, trusted
    assert len(integrity.TRUSTED_PATHS) == len(set(integrity.TRUSTED_PATHS))
    assert not any(entry.startswith(("reports", "evidence")) for entry in integrity.TRUSTED_PATHS)


def test_a_file_covered_twice_is_hashed_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file named on its own and again through its directory appears once, first."""
    _tree(tmp_path)
    monkeypatch.setattr(
        integrity,
        "TRUSTED_PATHS",
        ("docs/governance/example-record.md", "docs/governance/", "submission.yaml"),
    )

    files = [path.as_posix() for path in integrity.trusted_files(tmp_path)]

    assert files == ["docs/governance/example-record.md", "submission.yaml"]


def test_compare_names_changed_added_and_removed_files() -> None:
    """One finding per file, in path order, saying which of the three happened."""
    before = {"a.py": "1", "b.py": "2", "c.py": "3"}
    after = {"a.py": "1", "b.py": "9", "d.py": "4"}

    assert integrity.compare(before, after) == [
        "b.py changed during verification",
        "c.py was removed during verification",
        "d.py was added during verification",
    ]
    assert integrity.compare(before, dict(before)) == []


def test_a_changed_trusted_file_is_named_wherever_it_is(tmp_path: Path) -> None:
    """Record, rewrite a supplied document and the answer sheet behind the checks: both named."""
    _tree(tmp_path)
    snapshot = tmp_path.parent / f"{tmp_path.name}-snapshot.json"
    integrity.record(tmp_path, snapshot)

    (tmp_path / "docs/governance/example-record.md").write_text("# changed\n", encoding="utf-8")
    (tmp_path / "submission.yaml").write_text("answers: {x: 1}\n", encoding="utf-8")

    assert integrity.check(tmp_path, snapshot) == [
        "docs/governance/example-record.md changed during verification",
        "submission.yaml changed during verification",
    ]
    assert not snapshot.exists(), "the snapshot is consumed by the check"


def test_a_migration_added_during_the_run_is_named(tmp_path: Path) -> None:
    """A revision that appears in migrations/versions/ after the record is named as added."""
    _tree(tmp_path)
    snapshot = tmp_path.parent / f"{tmp_path.name}-migration.json"
    integrity.record(tmp_path, snapshot)

    (tmp_path / "migrations/versions/0002_late.py").write_text("x = 1\n", encoding="utf-8")

    assert integrity.check(tmp_path, snapshot) == [
        "migrations/versions/0002_late.py was added during verification"
    ]


def test_an_unchanged_tree_passes_and_the_run_output_is_not_covered(tmp_path: Path) -> None:
    """Record, then write the evidence and a report as a run does: no findings."""
    _tree(tmp_path)
    snapshot = tmp_path.parent / f"{tmp_path.name}-clean.json"

    integrity.record(tmp_path, snapshot)
    (tmp_path / "reports/security/scan.sarif").write_text('{"written": true}\n', encoding="utf-8")
    (tmp_path / "evidence/attack-dev.json").write_text('{"stages": {}}\n', encoding="utf-8")
    assert integrity.check(tmp_path, snapshot) == [], "the files a run writes are not covered"
    assert not snapshot.exists()


def test_a_missing_foreign_or_malformed_snapshot_is_a_tooling_error(tmp_path: Path) -> None:
    """No snapshot, another checkout's snapshot, or an unreadable one: an error, not a pass."""
    _tree(tmp_path)
    snapshot = tmp_path.parent / f"{tmp_path.name}-errors.json"

    with pytest.raises(integrity.IntegrityError, match="no integrity snapshot"):
        integrity.check(tmp_path, snapshot)

    other = tmp_path / "other"
    other.mkdir()
    _tree(other)
    integrity.record(other, snapshot)
    with pytest.raises(integrity.IntegrityError, match="was recorded for"):
        integrity.check(tmp_path, snapshot)

    snapshot.write_text("{not json", encoding="utf-8")
    with pytest.raises(integrity.IntegrityError, match="could not be read"):
        integrity.check(tmp_path, snapshot)
    snapshot.write_text('{"root": "x", "files": "nope"}', encoding="utf-8")
    with pytest.raises(integrity.IntegrityError, match="no file digests"):
        integrity.check(tmp_path, snapshot)


def test_the_snapshot_lives_outside_the_repository_per_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default path is in the system temporary directory and differs per checkout root."""
    monkeypatch.delenv(integrity.SNAPSHOT_VARIABLE, raising=False)
    first = integrity.snapshot_path(tmp_path / "one")
    second = integrity.snapshot_path(tmp_path / "two")

    assert first != second
    assert first.parent == second.parent
    assert not first.is_relative_to(tmp_path)
    assert first.name.startswith("coldline-verify-") and first.suffix == ".json"

    monkeypatch.setenv(integrity.SNAPSHOT_VARIABLE, str(tmp_path / "override.json"))
    assert integrity.snapshot_path(tmp_path / "one") == tmp_path / "override.json"


def test_the_command_line_records_then_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`record` exits 0 with the count; `check` exits 1 naming a change, 2 without a snapshot."""
    _tree(tmp_path)
    monkeypatch.setenv(integrity.SNAPSHOT_VARIABLE, str(tmp_path.parent / f"{tmp_path.name}.json"))

    assert integrity.main(["check", "--root", str(tmp_path)]) == 2
    assert "no integrity snapshot" in capsys.readouterr().err

    assert integrity.main(["record", "--root", str(tmp_path)]) == 0
    assert f"recorded {len(EXPECTED_ORDER)} trusted files" in capsys.readouterr().out
    (tmp_path / "tests/security/example.py").write_text("EXAMPLE = ('x',)\n", encoding="utf-8")
    assert integrity.main(["check", "--root", str(tmp_path)]) == 1
    assert "- tests/security/example.py changed during verification" in capsys.readouterr().err

    assert integrity.main(["record", "--root", str(tmp_path)]) == 0
    assert integrity.main(["check", "--root", str(tmp_path)]) == 0
    assert "every trusted file is as it was" in capsys.readouterr().out
