"""Coldline.

===================

File:              tests/contract/test_submission.py
Component:         Contract tests — Test Submission
Purpose:           Tests for the public answer and path checks for this Task's submission.
Interacts With:    Published interfaces and repository boundaries
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Compatibility, ownership, export safety
Tools:             Python 3.12, pytest
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.contract.submission_validation import (
    ALLOWED_PATHS,
    ANSWER_FIELDS,
    CHANGE_TYPES,
    MIGRATION_DIRECTORY,
    SubmissionError,
    _load_one_document,
    is_revision_file,
    main,
    validate_changed_paths,
    validate_submission,
)

ROOT = Path(__file__).parents[2]
SCHEMA = ROOT / "docs/contracts/submission.schema.json"
TEMPLATE = ROOT / "tests/fixtures/submission-template.yaml"
PERMITTED = [
    "src/common/audit_queries.py",
    "docs/student/audit-query-record.md",
    "submission.yaml",
]
NEW_MIGRATION = f"{MIGRATION_DIRECTORY}0f1e2d3c4b5a_example_revision.py"


def valid_answers(**overrides: Any) -> dict[str, object]:
    """Return a complete answer sheet in the published shape.

    Fictional format example: these values show the shape and state no result. The two plan
    nodes are node types that read no table, so no plan of this Task's query has them at
    the step that reads the audit table, and the after median is the higher one, which the
    public median row rejects.
    """
    answers: dict[str, Any] = {
        "before_plan_node": "Hash Join",
        "before_median_ms": 7.5,
        "change_type": "index",
        "after_plan_node": "Materialize",
        "after_median_ms": 9.25,
        "blocks_writes_during_change": True,
    }
    answers.update(overrides)
    return {"answers": answers}


def _task_root(tmp_path: Path, submission_text: str) -> Path:
    """Stage a minimal Task root the public verifier can validate."""
    (tmp_path / "docs/contracts").mkdir(parents=True)
    (tmp_path / "submission.yaml").write_text(submission_text, encoding="utf-8")
    (tmp_path / "submission-sample.yaml").write_text(
        (ROOT / "submission-sample.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "docs/contracts/submission.schema.json").write_text(
        SCHEMA.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


def test_a_complete_sheet_is_well_formed(tmp_path: Path) -> None:
    """The public schema accepts a complete sheet without judging its correctness."""
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers()))

    validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize("change_type", CHANGE_TYPES)
@pytest.mark.parametrize("blocks", [True, False])
def test_every_change_type_and_blocking_answer_is_well_formed(
    tmp_path: Path, change_type: str, blocks: bool
) -> None:
    """Both change types and both write-blocking answers pass; the schema prefers none."""
    sheet = valid_answers(change_type=change_type, blocks_writes_during_change=blocks)
    root = _task_root(tmp_path, yaml.safe_dump(sheet))

    validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize(
    "node",
    ["Hash Join", "Nested Loop", "Materialize", "CTE Scan", "WindowAgg", "Parallel Hash Join"],
)
def test_node_names_in_the_explain_form_are_well_formed(tmp_path: Path, node: str) -> None:
    """Any node name in EXPLAIN's form passes the format check; none is preferred."""
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers(before_plan_node=node)))

    validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize("median", [0.004, 3, 41.875, 600000])
def test_medians_are_positive_numbers_of_milliseconds(tmp_path: Path, median: float) -> None:
    """A whole or fractional positive number passes, up to ten minutes."""
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers(after_median_ms=median)))

    validate_submission(root / "submission.yaml", SCHEMA)


def test_blank_template_fails_with_field_address(tmp_path: Path) -> None:
    """An untouched answer sheet must identify the first incomplete field."""
    root = _task_root(tmp_path, TEMPLATE.read_text(encoding="utf-8"))

    with pytest.raises(SubmissionError, match="answers.before_plan_node is incomplete"):
        validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"before_plan_node": "hash join"}, "before_plan_node"),
        ({"before_plan_node": "Hash Join on audit_events"}, "before_plan_node"),
        ({"after_plan_node": "Nested Loop using ix_example"}, "after_plan_node"),
        ({"after_plan_node": "Materialize  (cost=0.00..1.00 rows=1 width=8)"}, "after_plan_node"),
        ({"after_plan_node": 7}, "after_plan_node"),
        ({"change_type": "both"}, "change_type"),
        ({"change_type": "Index"}, "change_type"),
        ({"before_median_ms": "12 ms"}, "before_median_ms"),
        ({"before_median_ms": -3.5}, "before_median_ms"),
        ({"after_median_ms": True}, "after_median_ms"),
        ({"after_median_ms": 600001}, "after_median_ms"),
        ({"blocks_writes_during_change": "false"}, "blocks_writes_during_change"),
        ({"blocks_writes_during_change": 0.5}, "blocks_writes_during_change"),
    ],
    ids=[
        "node-lowercase",
        "node-with-table",
        "node-with-index",
        "node-with-costs",
        "node-as-number",
        "unlisted-change-type",
        "capitalised-change-type",
        "median-as-text",
        "median-negative",
        "median-boolean",
        "median-too-large",
        "blocking-as-text",
        "blocking-as-number",
    ],
)
def test_values_outside_the_published_contract_are_rejected(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    """The public check must name the field it rejected, and reject the right ones."""
    sheet = valid_answers()
    answers = sheet["answers"]
    assert isinstance(answers, dict)
    answers.update(overrides)
    root = _task_root(tmp_path, yaml.safe_dump(sheet))

    with pytest.raises(SubmissionError, match=message):
        validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize("field", ANSWER_FIELDS)
def test_a_missing_or_blank_field_is_named(tmp_path: Path, field: str) -> None:
    """Each of the six answers is required, and a blank one is named as incomplete."""
    answers = dict(valid_answers()["answers"])  # type: ignore[arg-type]
    del answers[field]
    root = _task_root(tmp_path, yaml.safe_dump({"answers": answers}))
    with pytest.raises(SubmissionError, match=field):
        validate_submission(root / "submission.yaml", SCHEMA)

    blank = dict(valid_answers()["answers"])  # type: ignore[arg-type]
    blank[field] = {
        "before_median_ms": 0,
        "after_median_ms": 0,
        "blocks_writes_during_change": None,
    }.get(field, "")
    root = _task_root(tmp_path / "blank", yaml.safe_dump({"answers": blank}))
    with pytest.raises(SubmissionError, match=f"answers.{field} is incomplete"):
        validate_submission(root / "submission.yaml", SCHEMA)


@pytest.mark.parametrize(
    "field",
    ["verified", "instructor_approved", "speedup_ratio", "notes", "index_columns"],
)
def test_no_self_attestation_or_free_text_field_is_accepted(tmp_path: Path, field: str) -> None:
    """Reject a self-approval, a pass flag, a derived figure, a note, or an unasked detail."""
    answers = valid_answers()
    mapping = answers["answers"]
    assert isinstance(mapping, dict)
    mapping[field] = True
    root = _task_root(tmp_path, yaml.safe_dump(answers))

    with pytest.raises(SubmissionError, match="Additional properties"):
        validate_submission(root / "submission.yaml", SCHEMA)


def test_missing_answers_mapping_is_rejected(tmp_path: Path) -> None:
    """The answers mapping is required, not merely tolerated."""
    root = _task_root(tmp_path, "task: 4.9\n")

    with pytest.raises(SubmissionError, match="answers must be one mapping"):
        validate_submission(root / "submission.yaml", SCHEMA)


def test_exact_sample_copy_is_rejected(tmp_path: Path) -> None:
    """The published sample must not be accepted as a student submission."""
    root = _task_root(tmp_path, (ROOT / "submission-sample.yaml").read_text(encoding="utf-8"))

    with pytest.raises(SubmissionError, match="fictional sample"):
        validate_submission(
            root / "submission.yaml",
            SCHEMA,
            sample_path=root / "submission-sample.yaml",
        )


def test_public_entrypoint_reports_an_incomplete_answer_sheet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catch a verifier entrypoint that skips the real submission contract."""
    root = _task_root(tmp_path, TEMPLATE.read_text(encoding="utf-8"))

    assert main(root, changed_paths=[], format_only=True) == 1
    assert "answers.before_plan_node is incomplete" in capsys.readouterr().err


def test_public_entrypoint_rejects_the_sample_and_accepts_a_complete_sheet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`poe answers` applies the sample-copy check; a complete sheet of its own passes."""
    copied = _task_root(tmp_path / "copied", (ROOT / "submission-sample.yaml").read_text("utf-8"))
    assert main(copied, changed_paths=[], format_only=True) == 1
    assert "fictional sample" in capsys.readouterr().err

    own = _task_root(tmp_path / "own", yaml.safe_dump(valid_answers()))
    assert main(own, changed_paths=[], format_only=True) == 0
    assert main(own, changed_paths=list(PERMITTED)) == 0
    assert main(own, changed_paths=[*PERMITTED, NEW_MIGRATION], new_paths=[NEW_MIGRATION]) == 0


def test_public_entrypoint_reports_a_protected_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A change to the supplied audit store is named, not silently accepted."""
    root = _task_root(tmp_path, yaml.safe_dump(valid_answers()))

    assert main(root, changed_paths=["src/adapters/persistence/audit_store.py"]) == 1
    message = capsys.readouterr().err
    assert "protected path changed: src/adapters/persistence/audit_store.py" in message


def test_only_the_three_student_files_and_one_new_migration_are_permitted() -> None:
    """The three student files and one new revision pass; every supplied file is protected."""
    assert ALLOWED_PATHS == frozenset(PERMITTED)
    validate_changed_paths(list(PERMITTED))
    validate_changed_paths([*PERMITTED, NEW_MIGRATION], new_paths=[NEW_MIGRATION])
    validate_changed_paths([NEW_MIGRATION], new_paths=[NEW_MIGRATION])

    for protected in (
        "src/adapters/persistence/audit_store.py",
        "src/common/audit.py",
        "src/api/audit_lab.py",
        "src/api/audit_history.py",
        "src/api/audit_plan.py",
        "src/api/audit_trail.py",
        "infra/audit/sample-trails.json",
        "infra/audit/trail-baseline.json",
        "infra/postgres/001_opening_checkpoint.sql",
        "migrations/env.py",
        "migrations/script.py.mako",
        "alembic.ini",
        "docs/security/audit-events.md",
        "tests/contract/test_audit_query_contract.py",
        "tests/security/audit_verify.py",
        "tests/student/test_audit.py",
        ".github/workflows/task.yml",
        "security/gate.yaml",
        "compose.yaml",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "submission-sample.yaml",
    ):
        with pytest.raises(SubmissionError, match="protected path changed"):
            validate_changed_paths([protected])


@pytest.mark.parametrize(
    "existing",
    [
        "migrations/versions/e5f2a8c4d6b1_add_audit_events.py",
        "migrations/versions/f4c8d2a6b9e1_add_audit_exception_index.py",
        "migrations/versions/0001baseline_coldline_initialized_schema.py",
    ],
)
def test_an_existing_migration_may_not_change(existing: str) -> None:
    """A supplied revision that changed or went is protected, even beside a new one."""
    with pytest.raises(SubmissionError, match="protected path changed: .*an existing migration"):
        validate_changed_paths([existing])
    with pytest.raises(SubmissionError, match="an existing migration"):
        validate_changed_paths([existing, NEW_MIGRATION], new_paths=[NEW_MIGRATION])


def test_at_most_one_new_migration_is_permitted() -> None:
    """Two new revision files are two changes; the boundary permits one."""
    second = f"{MIGRATION_DIRECTORY}1a2b3c4d5e6f_another_revision.py"

    with pytest.raises(SubmissionError, match="more than one new migration"):
        validate_changed_paths([NEW_MIGRATION, second], new_paths=[NEW_MIGRATION, second])


@pytest.mark.parametrize(
    "misplaced",
    [
        "migrations/versions/notes.md",
        "migrations/versions/nested/0f1e2d3c4b5a_example_revision.py",
        "migrations/0f1e2d3c4b5a_example_revision.py",
    ],
)
def test_a_new_file_that_is_not_a_revision_is_protected(misplaced: str) -> None:
    """Only one .py file directly in migrations/versions/ counts as the new migration."""
    with pytest.raises(SubmissionError, match="protected path changed"):
        validate_changed_paths([misplaced], new_paths=[misplaced])


def test_the_revision_file_rule() -> None:
    """A revision file is a .py file whose parent is migrations/versions/ itself."""
    assert is_revision_file(NEW_MIGRATION)
    assert not is_revision_file("migrations/versions/notes.md")
    assert not is_revision_file("migrations/versions/nested/x.py")
    assert not is_revision_file("migrations/env.py")


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "answers: {value: first, value: second}\n",
        "answers: &answer {value: fictional}\n",
        "answers: *missing\n",
        "answers: {<<: {value: fictional}}\n",
        "answers: {value: 2026-09-04}\n",
        "answers: {value: !custom fictional}\n",
        "answers: {1: fictional}\n",
    ],
    ids=["duplicate-key", "anchor", "alias", "merge-key", "date", "custom-tag", "non-string-key"],
)
def test_non_json_yaml_constructs_are_rejected(tmp_path: Path, unsafe_text: str) -> None:
    """Reject restricted syntax before schema validation can mask a parser defect."""
    submission = tmp_path / "submission.yaml"
    submission.write_text(unsafe_text, encoding="utf-8")

    with pytest.raises(SubmissionError, match="restricted YAML"):
        _load_one_document(submission)


def test_multiple_yaml_documents_are_rejected(tmp_path: Path) -> None:
    """A second document cannot supply or replace the answer mapping."""
    submission = tmp_path / "submission.yaml"
    submission.write_text("answers: {}\n---\nanswers: {}\n", encoding="utf-8")

    with pytest.raises(SubmissionError, match="exactly one YAML mapping"):
        _load_one_document(submission)


def test_a_sheet_that_is_not_utf_8_is_a_submission_error(tmp_path: Path) -> None:
    """A sheet saved in another encoding gets the public error, not a Python traceback."""
    submission = tmp_path / "submission.yaml"
    submission.write_bytes("answers: {change_type: index}\n".encode("utf-16"))

    with pytest.raises(SubmissionError, match="restricted YAML"):
        _load_one_document(submission)


def test_the_schema_names_the_fields_and_the_allowed_values() -> None:
    """The schema's required fields and enum are the validator's constants, in order."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    answers = schema["properties"]["answers"]

    assert tuple(answers["required"]) == ANSWER_FIELDS
    assert tuple(answers["properties"]["change_type"]["enum"]) == CHANGE_TYPES
    assert answers["properties"]["blocks_writes_during_change"]["type"] == "boolean"
    assert schema["$defs"]["median_ms"]["type"] == "number"
    assert schema["$defs"]["median_ms"]["exclusiveMinimum"] == 0
    assert "Task 4.9" in schema["title"]


def test_the_template_fixture_is_a_blank_sheet_with_the_six_fields() -> None:
    """The fixture is the blank shape: empty strings, zeros and a null."""
    document = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))

    assert document == {
        "answers": {
            "before_plan_node": "",
            "before_median_ms": 0,
            "change_type": "",
            "after_plan_node": "",
            "after_median_ms": 0,
            "blocks_writes_during_change": None,
        }
    }


def test_the_sample_uses_the_published_shape() -> None:
    """The sample is schema-valid, and its after median is not lower than its before median."""
    document = _load_one_document(ROOT / "submission-sample.yaml")
    answers = document["answers"]

    validate_submission(ROOT / "submission-sample.yaml", SCHEMA)
    assert answers["change_type"] in CHANGE_TYPES
    assert answers["after_median_ms"] >= answers["before_median_ms"]
