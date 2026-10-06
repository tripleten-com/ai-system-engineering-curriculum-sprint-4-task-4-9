"""Coldline.

===================

File:              tests/contract/submission_validation.py
Component:         Contract tests — Submission Validation
Purpose:           Validate the Task 4.9 answer sheet (the two plan nodes, the change type, the two
                    medians and the write-blocking answer) and the permitted paths.
Interacts With:    Published interfaces and repository boundaries, tests/security/repository.py
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Compatibility, ownership, export safety
Tools:             Python 3.12, pytest

Two entrypoints share this module. ``poe answers`` runs it with ``--format-only`` and checks
the answer sheet alone: one plain YAML mapping whose ``answers`` carries
``before_plan_node`` and ``after_plan_node`` (a plan node type as EXPLAIN prints it),
``change_type`` (``index`` or ``rewrite``), ``before_median_ms`` and ``after_median_ms``
(positive numbers of milliseconds) and ``blocks_writes_during_change`` (``true`` or
``false``), and that the sheet is not a copy of the fictional sample. ``poe submission``
(inside ``poe verify``) runs it in full, which adds the permitted-path boundary: the diff
from the merge base touches only ``src/common/audit_queries.py``,
``docs/student/audit-query-record.md`` and ``submission.yaml``, plus at most one file it
adds directly under ``migrations/versions/``; no migration that existed at the starting
checkpoint may change or go.

Nothing here judges which values are right. Whether the plan nodes, the change type and the
write-blocking answer agree with the plans ``poe verify`` observes and with the diff, and
whether the after median is lower, are rows of ``tests/contract/test_audit_query_contract.py``.
"""

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from tests.security import repository

# The three fixed files every Task 4.9 pull request may change. One new revision file
# directly under migrations/versions/ is permitted besides them (MIGRATION_DIRECTORY).
ALLOWED_PATHS = frozenset(
    {
        "docs/student/audit-query-record.md",
        "src/common/audit_queries.py",
        "submission.yaml",
    }
)
# Task 4.9 grants no directory prefix: the one new migration is a counted exception with its
# own rule (`validate_changed_paths`), not a prefix that would admit any change under it.
ALLOWED_PREFIXES: tuple[str, ...] = ()
MIGRATION_DIRECTORY = "migrations/versions/"
MAX_NEW_MIGRATIONS = 1
ANSWER_FIELDS = (
    "before_plan_node",
    "before_median_ms",
    "change_type",
    "after_plan_node",
    "after_median_ms",
    "blocks_writes_during_change",
)
CHANGE_TYPES = ("index", "rewrite")
PLAN_NODE_FIELDS = ("before_plan_node", "after_plan_node")


_JSON_YAML_TAGS = frozenset(
    {
        "tag:yaml.org,2002:map",
        "tag:yaml.org,2002:seq",
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:null",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
    }
)


class RestrictedYamlLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Load the Task's small YAML profile without YAML-only conveniences."""

    def compose_node(self, parent: object, index: object) -> yaml.Node:
        """Compose one node, refusing an alias or an anchor where it appears."""
        # A prior anchor is already rejected below, but deny aliases directly too.
        if self.check_event(yaml.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML aliases are not permitted",
                event.start_mark,
            )
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML anchors are not permitted",
                event.start_mark,
            )
        return super().compose_node(parent, index)

    def construct_object(self, node: yaml.Node, deep: bool = False) -> object:
        """Construct one value, refusing any tag outside the JSON-compatible set."""
        if node.tag not in _JSON_YAML_TAGS:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "non-JSON YAML tags are not permitted",
                node.start_mark,
            )
        return super().construct_object(node, deep=deep)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, object]:
        """Construct one mapping with string keys, refusing merge keys and repeated keys."""
        mapping: dict[str, object] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "YAML merge keys are not permitted",
                    key_node.start_mark,
                )
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "YAML mapping keys must be strings",
                    key_node.start_mark,
                )
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"duplicate YAML key: {key}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class SubmissionError(ValueError):
    """Report one actionable public-verification failure."""


def main(
    root: Path | None = None,
    *,
    changed_paths: list[str] | None = None,
    new_paths: list[str] | None = None,
    format_only: bool = False,
) -> int:
    """Validate the answer sheet, and unless ``format_only``, the permitted-path boundary.

    The optional arguments keep this entrypoint testable without changing the process
    working directory or creating a temporary Git repository. ``new_paths`` names the
    changed paths that did not exist at the starting checkpoint; with ``changed_paths``
    given and ``new_paths`` left out, no changed path counts as new.
    """
    task_root = Path.cwd() if root is None else root
    try:
        validate_submission(
            task_root / "submission.yaml",
            task_root / "docs/contracts/submission.schema.json",
            sample_path=task_root / "submission-sample.yaml",
            task_root=task_root,
        )
        if not format_only:
            if changed_paths is None:
                changed = _changed_paths(task_root)
                new = new_migration_paths(task_root, changed)
            else:
                changed = changed_paths
                new = list(new_paths or [])
            validate_changed_paths(changed, new_paths=new)
    except (SubmissionError, RuntimeError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 1
    if format_only:
        print("Task 4.9 answer format check passed.")
    else:
        print("Task 4.9 answer and permitted-path verification passed.")
    return 0


def _incomplete(answers: dict[str, Any]) -> str | None:
    """Return the address of the first blank answer, or None when none is blank.

    The blank sheet names the field left empty before the schema reports a format: an empty
    string, a zero, or a null.
    """
    for field in ANSWER_FIELDS:
        if field not in answers:
            continue
        value = answers[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            return f"answers.{field}"
        if isinstance(value, int | float) and not isinstance(value, bool) and value == 0:
            return f"answers.{field}"
    return None


def _node_form(answers: dict[str, Any]) -> str | None:
    """Return a finding when a plan node answer carries more than the node type."""
    for field in PLAN_NODE_FIELDS:
        value = answers.get(field)
        if not isinstance(value, str):
            continue
        words = value.split()
        if "on" in words or "using" in words or "(" in value:
            return (
                f"answers.{field}: record the node type alone, the words at the start of the "
                "plan line before `using`, `on` and the costs"
            )
    return None


def validate_submission(
    submission_path: Path,
    schema_path: Path,
    *,
    sample_path: Path | None = None,
    task_root: Path | None = None,
) -> None:
    """Validate YAML shape, placeholders, the schema, and sample-copy behavior.

    ``task_root`` names the repository that owns the published contracts. It defaults
    to the answer sheet's own directory, which is correct for a student checkout. A
    curriculum-owned evaluator validating a sheet stored elsewhere passes the trusted
    Task root explicitly.
    """
    submission = _load_one_document(submission_path)
    answers = submission.get("answers") if isinstance(submission, dict) else None
    if not isinstance(answers, dict):
        raise SubmissionError("answers must be one mapping")

    incomplete = _incomplete(answers)
    if incomplete is not None:
        raise SubmissionError(f"{incomplete} is incomplete")
    node_form = _node_form(answers)
    if node_form is not None:
        raise SubmissionError(node_form)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(submission), key=lambda error: list(error.path)
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "submission"
        raise SubmissionError(f"{location}: {error.message}")

    if sample_path is not None and submission == _load_one_document(sample_path):
        raise SubmissionError("submission must not copy the fictional sample answers")


def is_revision_file(path: str) -> bool:
    """Return whether a path is one Python file directly in ``migrations/versions/``."""
    candidate = PurePosixPath(path)
    return f"{candidate.parent.as_posix()}/" == MIGRATION_DIRECTORY and candidate.suffix == ".py"


def validate_changed_paths(
    paths: list[str],
    permitted: frozenset[str] = ALLOWED_PATHS,
    *,
    new_paths: Iterable[str] = (),
) -> None:
    """Reject changed paths outside the permitted surfaces.

    A changed path under ``migrations/versions/`` is permitted only when it is in
    ``new_paths`` (it did not exist at the starting checkpoint), is one Python file directly
    in that directory, and is the only such path: no existing migration may change or go.
    """
    normalized = {PurePosixPath(path.replace("\\", "/")).as_posix() for path in paths}
    new = {PurePosixPath(path.replace("\\", "/")).as_posix() for path in new_paths}
    migrations = sorted(path for path in normalized if path.startswith(MIGRATION_DIRECTORY))
    existing = [path for path in migrations if path not in new]
    if existing:
        raise SubmissionError(
            f"protected path changed: {', '.join(existing)} (an existing migration; "
            "add one new revision instead of changing or removing one)"
        )
    misplaced = [path for path in migrations if not is_revision_file(path)]
    if misplaced:
        raise SubmissionError(
            f"protected path changed: {', '.join(misplaced)} (a new migration is one .py "
            f"file directly in {MIGRATION_DIRECTORY})"
        )
    if len(migrations) > MAX_NEW_MIGRATIONS:
        raise SubmissionError(
            f"more than one new migration: {', '.join(migrations)}; this Task permits one"
        )
    protected = sorted(
        path
        for path in normalized - permitted - set(migrations)
        if not path.startswith(ALLOWED_PREFIXES)
    )
    if protected:
        raise SubmissionError(f"protected path changed: {', '.join(protected)}")


def new_migration_paths(root: Path, changed: Iterable[str]) -> list[str]:
    """Return the changed paths under ``migrations/versions/`` the starting checkpoint lacked."""
    return [
        path
        for path in changed
        if path.startswith(MIGRATION_DIRECTORY) and repository.baseline_text(root, path) is None
    ]


def _changed_paths(root: Path) -> list[str]:
    """Return changes since the commit this checkout branched from."""
    try:
        return repository.changed_paths(root)
    except repository.RepositoryError as exc:
        raise RuntimeError("Git history is unavailable for protected-path validation") from exc


def _load_one_document(path: Path) -> dict[str, Any]:
    """Load exactly one plain JSON-compatible YAML mapping.

    A file that is not UTF-8 is reported the same way as one that is not the
    restricted YAML profile: as a public verification failure, not a traceback.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SubmissionError(
            f"{path.name} is missing; restore it from the starting checkpoint"
        ) from exc
    except UnicodeDecodeError as exc:
        raise SubmissionError(f"{path.name} must contain UTF-8 restricted YAML") from exc
    try:
        documents = list(yaml.load_all(text, Loader=RestrictedYamlLoader))
    except yaml.YAMLError as exc:
        raise SubmissionError(f"{path.name} must contain UTF-8 restricted YAML") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise SubmissionError(f"{path.name} must contain exactly one YAML mapping")
    return documents[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate the Task 4.9 submission.")
    parser.add_argument(
        "--format-only",
        action="store_true",
        help="check the answer sheet's format only (what `poe answers` runs)",
    )
    arguments = parser.parse_args()
    raise SystemExit(main(format_only=arguments.format_only))
