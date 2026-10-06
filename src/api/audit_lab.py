"""Coldline.

===================

File:              src/api/audit_lab.py
Component:         Audit query lab composition root
Purpose:           Load the supplied large audit history; plan, time and compare the trail query;
                    print the audit table's index sizes; run `poe verify`'s fresh-database check.
Interacts With:    PostgreSQL, src/common/audit_queries.py, src/api/audit_history.py,
                    src/api/audit_plan.py, src/adapters/persistence/audit_store.py, alembic.ini,
                    migrations/, pyproject.toml (`poe audit-*`), tests/security/audit_verify.py
Sprint/Task:       Sprint 4 — Project 4 / Task 4.9
Concepts:          Query plans, warm-up and median timing, reversible migrations, fresh databases
Tools:             Python 3.12, PostgreSQL, asyncpg, Alembic

Every command runs inside the API container (``docker compose exec -T api python -m
api.audit_lab <command>``), where the database is reachable and ``COLDLINE_DATABASE_URL`` is
set, the way ``poe audit-trail`` and ``poe migrate`` run; the host never opens a database
port. The image carries ``src/`` and ``migrations/`` as they were when it was built, so a
changed query or a new migration reaches these commands after ``poe rebuild-api``.

- ``seed`` (``poe audit-seed-large``) loads the supplied history into the stack's database
  in one transaction and prints the event count. It is idempotent: a database that holds
  the history already is left as it is.
- ``explain <exception_id>`` (``poe audit-explain``) refreshes the table's statistics,
  prints ``EXPLAIN (ANALYZE, BUFFERS)`` of ``trail_for_exception`` and lists the audit
  table's indexes.
- ``benchmark <exception_id>`` (``poe audit-benchmark``) refreshes the statistics, runs
  the query once to warm up and ``BENCHMARK_RUNS`` times more, and prints the median.
- ``compare`` (``poe audit-trail-compare``) reads each sample exception's trail through the
  audit store, as ``poe audit-trail`` does, by its id as stored and in upper case, and
  compares both with the stored baseline.
- ``index-size`` (``poe audit-index-size``) prints the size of every index on the table.
- ``verify-run`` is ``poe verify``'s procedure, read by ``tests/security/audit_verify.py``.
  It never touches the stack's database: it creates ``VERIFY_DATABASE`` fresh beside it,
  builds the schema and migrates it to the supplied head, loads the history, observes the
  supplied query, applies every new migration (upgrade, downgrade, upgrade), observes the
  current query, adds one probe trail and reads it with both queries, prints one JSON
  report line and drops the database again.

Every plan and timing is taken in a session with ``audit_plan.PLANNER_SETTINGS`` applied,
after ``ANALYZE``, so one table gives one plan.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from alembic.config import Config
from alembic.script import ScriptDirectory

from adapters.persistence import PostgresAuditStore
from adapters.persistence.audit_store import record_from_row
from api import audit_history, audit_plan
from api.config import ApiSettings
from api.initialize import ALEMBIC_CONFIG, SCHEMA_FILES
from common.audit import AuditRecord
from common.audit_queries import trail_for_exception

# The database `verify-run` creates, uses and drops; never the stack's own.
VERIFY_DATABASE = "coldline_audit_verify"
BENCHMARK_RUNS = 15
ALEMBIC_TIMEOUT_SECONDS = 900
OUTPUT_TAIL_CHARACTERS = 2000
# The transaction lock a load takes, so two `poe audit-seed-large` runs load the history once.
SEED_LOCK = 4909
_DATABASE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
# The probe trail `verify-run` adds to its own database once the plan after the change is
# observed: longer than any sample trail, with groups of events recorded in the same second
# whose audit_ids run against the order the table stores them in. The supplied query's
# reading of it, audit_id included, is the reference for the current query's.
PROBE_EXCEPTION_ID = "exc-7d3f9a2c-4e1b-5c80-9a6d-2b8e4f1c0a59"
PROBE_EVENTS = 40
PROBE_TIE_STARTS = (2, 10, 18, 26, 34)
PROBE_TIE_SIZE = 3
# Far above every audit_id the supplied history takes.
PROBE_FIRST_AUDIT_ID = 900_000_001
PROBE_START = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
# audit_id is an identity column generated always, so explicit ids override it.
PROBE_INSERT = """
    INSERT INTO audit_events (audit_id, exception_id, event, trace_id, recorded_at, details)
    OVERRIDING SYSTEM VALUE
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""
INDEX_QUERY = """
    SELECT tablename, indexname, indexdef FROM pg_indexes
    WHERE schemaname = 'public'
    ORDER BY tablename, indexname
"""
COLUMN_QUERY = """
    SELECT table_name, column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = 'public'
    ORDER BY table_name, ordinal_position
"""
INDEX_SIZE_QUERY = """
    SELECT c.relname AS index_name,
           pg_relation_size(c.oid) AS bytes,
           pg_size_pretty(pg_relation_size(c.oid)) AS size
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
    WHERE i.indrelid = 'audit_events'::regclass
    ORDER BY c.relname
"""


@dataclass(frozen=True)
class AlembicRun:
    """One Alembic command's outcome."""

    ok: bool
    stdout: str
    stderr: str

    def detail(self) -> str:
        """Return the end of what the command printed, for a report."""
        return (self.stdout + self.stderr)[-OUTPUT_TAIL_CHARACTERS:]


@dataclass(frozen=True)
class SeedOutcome:
    """What a load did: whether it loaded now, and how many events the table holds."""

    loaded_now: bool
    table_events: int


@dataclass(frozen=True)
class TrailComparison:
    """Each sampled trail's differences from the baseline, by the id asked, and what was read."""

    differences: dict[str, list[str]]
    exceptions: int
    events_read: int


# Reads one exception's trail for the id it is asked with, as the audit store does.
TrailReader = Callable[[str], Awaitable[Sequence[AuditRecord]]]


def database_url(base: str, database: str) -> str:
    """Return ``base`` pointed at another database on the same server."""
    if not _DATABASE_NAME.fullmatch(database):
        raise ValueError(f"{database!r} is not a plain database name")
    parts = urlsplit(base)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, ""))


def _settings() -> ApiSettings:
    """Return the API's settings from the container environment."""
    return ApiSettings()  # type: ignore[call-arg]  # values come from the protected environment


def _checked(exception_id: str) -> str:
    """Return the id when it has the API's shape, or refuse it with the reason."""
    if not _IDENTITY.fullmatch(exception_id):
        raise SystemExit(f"{exception_id!r} is not an exception id; README.md names the one to use")
    return exception_id


async def pin_planner(connection: Any, extra: Sequence[tuple[str, str]] = ()) -> None:
    """Apply the supplied planner settings, and any extra ones, to this session."""
    for name, value in (*audit_plan.PLANNER_SETTINGS, *extra):
        await connection.execute("SELECT set_config($1, $2, false)", name, value)


async def _analyze(connection: Any) -> None:
    """Refresh the audit table's statistics with the pinned statistics target."""
    await connection.execute(f"ANALYZE {audit_plan.AUDIT_TABLE}")


async def _history_present(connection: Any, samples: audit_history.SampleTrails) -> bool:
    """Return whether the database holds the supplied history already."""
    present = await connection.fetchval(
        "SELECT count(*) FROM audit_events WHERE exception_id = $1",
        samples.measurement_exception_id,
    )
    return bool(present)


async def seed_history(url: str) -> SeedOutcome:
    """Load the supplied history once; a database that holds it already is left as it is.

    Two loads at once take the same transaction lock in turn, and the second finds the
    history present when it gets the lock, so the history is never loaded twice.
    """
    samples = audit_history.load_sample_trails()
    connection = await asyncpg.connect(dsn=url)
    loaded_now = False
    try:
        await pin_planner(connection)
        if not await _history_present(connection, samples):
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1::bigint)", SEED_LOCK)
                if not await _history_present(connection, samples):
                    history = audit_history.history_events(samples)
                    await connection.copy_records_to_table(
                        audit_plan.AUDIT_TABLE,
                        records=(event.copy_record() for event in history),
                        columns=list(audit_history.COPY_COLUMNS),
                    )
                    loaded_now = True
        if loaded_now:
            await connection.execute(f"VACUUM (ANALYZE) {audit_plan.AUDIT_TABLE}")
        total = await connection.fetchval("SELECT count(*) FROM audit_events")
    finally:
        await connection.close()
    return SeedOutcome(loaded_now=loaded_now, table_events=int(total))


async def explain(url: str, exception_id: str) -> tuple[list[str], list[tuple[str, str]], int]:
    """Return the plan text, the audit table's indexes, and how many events the query reads.

    The events are counted through the trail query itself, so an id the query matches in
    any letter case is counted as the query reads it.
    """
    connection = await asyncpg.connect(dsn=url)
    try:
        await pin_planner(connection)
        await _analyze(connection)
        sql, parameters = trail_for_exception(exception_id)
        trail = sql.strip().rstrip(";")
        events = await connection.fetchval(
            f"SELECT count(*) FROM (\n{trail}\n) AS trail", *parameters
        )
        rows = await connection.fetch(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", *parameters)
        indexes = await connection.fetch(INDEX_QUERY)
    finally:
        await connection.close()
    listed = [
        (str(row["indexname"]), str(row["indexdef"]))
        for row in indexes
        if row["tablename"] == audit_plan.AUDIT_TABLE
    ]
    return [str(row[0]) for row in rows], listed, int(events)


async def benchmark(url: str, exception_id: str, runs: int = BENCHMARK_RUNS) -> dict[str, Any]:
    """Run the trail query once to warm up and ``runs`` times more; return the timings."""
    connection = await asyncpg.connect(dsn=url)
    try:
        await pin_planner(connection)
        await _analyze(connection)
        sql, parameters = trail_for_exception(exception_id)
        warm = await connection.fetch(sql, *parameters)
        timings: list[float] = []
        for _ in range(runs):
            started = time.perf_counter()
            await connection.fetch(sql, *parameters)
            timings.append(time.perf_counter() - started)
    finally:
        await connection.close()
    return {
        "events": len(warm),
        "runs": runs,
        "median_ms": audit_plan.median_ms(timings),
        "fastest_ms": round(min(timings) * 1000.0, 3),
        "slowest_ms": round(max(timings) * 1000.0, 3),
    }


async def compare_samples(read: TrailReader) -> TrailComparison:
    """Read each sample trail by its id as stored and in upper case; compare both.

    Operators paste ids in either case, and the trail query matches them without regard
    to letter case, so both readings must equal the same stored baseline.
    """
    baseline = audit_history.load_baseline()
    differences: dict[str, list[str]] = {}
    events_read = 0
    for exception_id, expected in baseline.items():
        for asked in (exception_id, exception_id.upper()):
            records = await read(asked)
            events_read += len(records)
            actual = [audit_history.comparable_record(record) for record in records]
            key = exception_id if asked == exception_id else f"{exception_id} (asked as {asked})"
            differences[key] = audit_history.trail_differences(expected, actual)
    return TrailComparison(differences, len(baseline), events_read)


async def compare_trails(url: str) -> TrailComparison:
    """Read each sample trail through the audit store and compare it with the baseline."""
    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=2)
    if pool is None:  # pragma: no cover - asyncpg returns a pool or raises
        raise RuntimeError("could not create a PostgreSQL connection pool")
    try:
        return await compare_samples(PostgresAuditStore(pool).trail)
    finally:
        await pool.close()


def probe_rows() -> list[tuple[int, str, str, str, datetime, str]]:
    """Return the probe trail's rows, with every column, in the order they are inserted.

    Event ``n`` takes audit_id ``PROBE_FIRST_AUDIT_ID + n`` and is recorded ``n`` seconds
    after ``PROBE_START``, except that each group of ``PROBE_TIE_SIZE`` events from a
    ``PROBE_TIE_STARTS`` entry shares its first event's second. Each group is inserted
    highest audit_id first, so the table stores it against audit_id order.
    """
    tied = {start + offset: start for start in PROBE_TIE_STARTS for offset in range(PROBE_TIE_SIZE)}
    rows = [
        (
            PROBE_FIRST_AUDIT_ID + number,
            PROBE_EXCEPTION_ID,
            "summary_read",
            hashlib.sha256(f"coldline:audit-probe:{number}".encode()).hexdigest()[:32],
            PROBE_START + timedelta(seconds=tied.get(number, number)),
            json.dumps(
                {"subject": f"user:dispatcher-{number % 40:02d}", "role": "dispatcher"},
                sort_keys=True,
            ),
        )
        for number in range(PROBE_EVENTS)
    ]
    return sorted(rows, key=lambda row: (row[4], -row[0]))


def _with_audit_id(records: Sequence[AuditRecord]) -> list[dict[str, object]]:
    """Return each record's compared fields and its audit_id."""
    return [
        {**audit_history.comparable_record(record), "audit_id": record.audit_id}
        for record in records
    ]


async def compare_probe(read: TrailReader, supplied: TrailReader) -> dict[str, list[str]]:
    """Read the probe trail by its id as stored and in upper case; compare both readings.

    ``supplied`` runs the query as Task 4.9 supplies it, and its reading is the reference,
    audit_id included, so two same-second events returned the other way round differ.
    """
    differences: dict[str, list[str]] = {}
    for asked in (PROBE_EXCEPTION_ID, PROBE_EXCEPTION_ID.upper()):
        expected = _with_audit_id(await supplied(asked))
        if len(expected) != PROBE_EVENTS:
            raise RuntimeError(f"the supplied query read {len(expected)} probe events")
        actual = _with_audit_id(await read(asked))
        key = f"probe {PROBE_EXCEPTION_ID}"
        if asked != PROBE_EXCEPTION_ID:
            key = f"{key} (asked as {asked})"
        differences[key] = audit_history.trail_differences(expected, actual)
    return differences


async def probe_trails(url: str) -> dict[str, list[str]]:
    """Add the probe trail, then compare the audit store's reading with the supplied query's."""
    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=2)
    if pool is None:  # pragma: no cover - asyncpg returns a pool or raises
        raise RuntimeError("could not create a PostgreSQL connection pool")

    async def supplied(exception_id: str) -> list[AuditRecord]:
        rows = await pool.fetch(audit_plan.SUPPLIED_TRAIL_SQL, exception_id)
        return [record_from_row(row) for row in rows]

    try:
        await pool.executemany(PROBE_INSERT, probe_rows())
        return await compare_probe(PostgresAuditStore(pool).trail, supplied)
    finally:
        await pool.close()


async def index_sizes(url: str) -> tuple[list[tuple[str, int, str]], str]:
    """Return every index on the audit table with its size, and the table's own size."""
    connection = await asyncpg.connect(dsn=url)
    try:
        rows = await connection.fetch(INDEX_SIZE_QUERY)
        table = await connection.fetchval(
            "SELECT pg_size_pretty(pg_relation_size('audit_events'::regclass))"
        )
    finally:
        await connection.close()
    sizes = [(str(row["index_name"]), int(row["bytes"]), str(row["size"])) for row in rows]
    return sizes, str(table)


# --- verify-run ----------------------------------------------------------------------------


def alembic(database: str, *arguments: str) -> AlembicRun:
    """Run one Alembic command against ``database``, through the supplied environment."""
    try:
        completed = subprocess.run(
            ["alembic", "-x", f"database={database}", *arguments],
            capture_output=True,
            text=True,
            timeout=ALEMBIC_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AlembicRun(ok=False, stdout="", stderr=f"alembic could not run: {exc}")
    return AlembicRun(completed.returncode == 0, completed.stdout, completed.stderr)


def migration_chain() -> dict[str, Any]:
    """Return the revision graph's heads and every revision Task 4.9 did not supply."""
    try:
        script = ScriptDirectory.from_config(Config(ALEMBIC_CONFIG))
        heads = sorted(script.get_heads())
        new: dict[str, object] = {}
        for revision in script.walk_revisions(base="base", head="heads"):
            if revision.revision in audit_plan.SUPPLIED_REVISIONS:
                continue
            down = revision.down_revision
            new[revision.revision] = down if down is None or isinstance(down, str) else list(down)
    except Exception as exc:  # a chain Alembic cannot read is itself the finding
        return {"heads": [], "new_revisions": {}, "error": f"{type(exc).__name__}: {exc}"}
    return {"heads": heads, "new_revisions": new, "error": None}


async def _fresh_database(base_url: str) -> None:
    """Drop and create the verification database, then build the initializer's schema in it."""
    admin = await asyncpg.connect(dsn=base_url)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {VERIFY_DATABASE} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {VERIFY_DATABASE}")
    finally:
        await admin.close()
    connection = await asyncpg.connect(dsn=database_url(base_url, VERIFY_DATABASE))
    try:
        for schema_file in SCHEMA_FILES:
            await connection.execute(Path(schema_file).read_text(encoding="utf-8"))
    finally:
        await connection.close()


async def _drop_database(base_url: str) -> None:
    """Drop the verification database."""
    admin = await asyncpg.connect(dsn=base_url)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {VERIFY_DATABASE} WITH (FORCE)")
    finally:
        await admin.close()


async def _schema_state(url: str) -> dict[str, Any]:
    """Return the database's revision, every public index, and every public column."""
    connection = await asyncpg.connect(dsn=url)
    try:
        versions = await connection.fetch("SELECT version_num FROM alembic_version")
        indexes = await connection.fetch(INDEX_QUERY)
        columns = await connection.fetch(COLUMN_QUERY)
    finally:
        await connection.close()
    return {
        "versions": sorted(str(row["version_num"]) for row in versions),
        "indexes": {
            str(row["indexname"]): {
                "table": str(row["tablename"]),
                "definition": str(row["indexdef"]),
            }
            for row in indexes
        },
        "columns": [
            f"{row['table_name']}.{row['column_name']} {row['data_type']} "
            f"nullable={row['is_nullable']} default={row['column_default']}"
            for row in columns
        ],
    }


async def _plan(
    connection: Any, sql: str, parameters: Sequence[object], extra: Sequence[tuple[str, str]]
) -> list[audit_plan.AuditRead]:
    """Plan one query with the given extra settings, inside a transaction, and find its reads."""
    async with connection.transaction():
        for name, value in extra:
            await connection.execute("SELECT set_config($1, $2, true)", name, value)
        document = await connection.fetchval(f"EXPLAIN (FORMAT JSON) {sql}", *parameters)
    loaded = json.loads(document) if isinstance(document, str) else document
    return audit_plan.audit_reads(loaded)


async def _observe(url: str, sql: str, parameters: Sequence[object]) -> dict[str, Any]:
    """Return the measured plan's audit reads and every index-driven form the planner uses."""
    connection = await asyncpg.connect(dsn=url)
    try:
        await pin_planner(connection)
        await _analyze(connection)
        analyzed = await connection.fetchval(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", *parameters
        )
        reads = audit_plan.audit_reads(
            json.loads(analyzed) if isinstance(analyzed, str) else analyzed
        )
        forms: set[str] = set()
        for _, extra in audit_plan.INDEX_FORM_VARIANTS:
            for read in await _plan(connection, sql, parameters, extra):
                if read.index_driven:
                    forms.add(read.node)
    finally:
        await connection.close()
    return {"reads": [read.as_dict() for read in reads], "index_forms": sorted(forms)}


async def _trusted_trails(url: str) -> dict[str, list[str]]:
    """Compare each sample trail as the supplied query reads it with the stored baseline."""
    connection = await asyncpg.connect(dsn=url)

    async def supplied(exception_id: str) -> list[AuditRecord]:
        rows = await connection.fetch(audit_plan.SUPPLIED_TRAIL_SQL, exception_id)
        return [record_from_row(row) for row in rows]

    try:
        return (await compare_samples(supplied)).differences
    finally:
        await connection.close()


def _attempt[T](report: dict[str, Any], name: str, action: Callable[[], T]) -> T | None:
    """Run one observation; record its failure in the report instead of stopping the run."""
    try:
        return action()
    except Exception as exc:  # the report carries every failure to the row that reads it
        report["errors"][name] = f"{type(exc).__name__}: {exc}"
        return None


def verify_run(keep: bool = False) -> dict[str, Any]:
    """Run `poe verify`'s fresh-database procedure and return its report.

    Fatal steps (the database, the supplied schema, the history) end the run with
    ``fatal`` set; everything after them is observed and recorded even when one part
    fails, so each assessed row reads the evidence it needs and nothing more.
    """
    base_url = _settings().database_url
    url = database_url(base_url, VERIFY_DATABASE)
    measured = audit_history.MEASUREMENT_EXCEPTION_ID
    report: dict[str, Any] = {
        "database": VERIFY_DATABASE,
        "supplied_head": audit_plan.SUPPLIED_HEAD,
        "measurement_exception_id": measured,
        "steps": {},
        "errors": {},
        "fatal": None,
    }

    def step(name: str, run: AlembicRun | None, skipped: str = "") -> bool:
        if run is None:
            report["steps"][name] = {"ok": True, "skipped": True, "detail": skipped}
            return True
        report["steps"][name] = {"ok": run.ok, "skipped": False, "detail": run.detail()}
        return run.ok

    try:
        _attempt(report, "create", lambda: asyncio.run(_fresh_database(base_url)))
        if "create" in report["errors"]:
            report["fatal"] = "the verification database could not be created"
            return report
        supplied = alembic(VERIFY_DATABASE, "upgrade", audit_plan.SUPPLIED_HEAD)
        if not step("migrate_supplied", supplied):
            report["fatal"] = (
                "the migrations did not apply to a fresh database up to the supplied head; "
                "a revision file Alembic cannot load stops every migration"
            )
            return report
        seeded = _attempt(report, "seed", lambda: asyncio.run(seed_history(url)))
        if seeded is None:
            report["fatal"] = "the supplied history could not be loaded"
            return report
        report["events"] = seeded.table_events
        report["chain"] = migration_chain()
        report["state_supplied"] = _attempt(
            report, "state_supplied", lambda: asyncio.run(_schema_state(url))
        )
        report["plan_before"] = _attempt(
            report,
            "plan_before",
            lambda: asyncio.run(_observe(url, audit_plan.SUPPLIED_TRAIL_SQL, [measured])),
        )
        report["trails_before"] = _attempt(
            report, "trails_before", lambda: asyncio.run(_trusted_trails(url))
        )

        new = bool(report["chain"]["new_revisions"])
        upgrade = alembic(VERIFY_DATABASE, "upgrade", "head") if new else None
        step("upgrade", upgrade, "no new revision")
        report["state_after_upgrade"] = _attempt(
            report, "state_after_upgrade", lambda: asyncio.run(_schema_state(url))
        )
        report["plan_after"] = _attempt(
            report,
            "plan_after",
            lambda: asyncio.run(_observe(url, *trail_for_exception(measured))),
        )
        report["trails_after"] = _attempt(
            report, "trails_after", lambda: asyncio.run(compare_trails(url)).differences
        )
        probe = _attempt(report, "trails_probe", lambda: asyncio.run(probe_trails(url)))
        if isinstance(report["trails_after"], dict):
            unread = [f"not read: {report['errors'].get('trails_probe')}"]
            report["trails_after"].update(probe or {f"probe {PROBE_EXCEPTION_ID}": unread})
        if new:
            offline = alembic(
                VERIFY_DATABASE, "upgrade", f"{audit_plan.SUPPLIED_HEAD}:head", "--sql"
            )
            report["offline_sql"] = offline.stdout if offline.ok else None
            step("offline_sql", offline)
            step("downgrade", alembic(VERIFY_DATABASE, "downgrade", audit_plan.SUPPLIED_HEAD))
            report["state_after_downgrade"] = _attempt(
                report, "state_after_downgrade", lambda: asyncio.run(_schema_state(url))
            )
            step("reupgrade", alembic(VERIFY_DATABASE, "upgrade", "head"))
            report["state_after_reupgrade"] = _attempt(
                report, "state_after_reupgrade", lambda: asyncio.run(_schema_state(url))
            )
        return report
    finally:
        if not keep:
            _attempt(report, "drop", lambda: asyncio.run(_drop_database(base_url)))


# --- command line --------------------------------------------------------------------------


def _print_seed(outcome: SeedOutcome) -> None:
    """Print what the load did."""
    events = audit_history.HISTORY_EVENTS
    if outcome.loaded_now:
        print(f"loaded the supplied audit history: {events} events")
    else:
        print(f"the supplied audit history is already loaded: {events} events")
    print(f"audit events in the table: {outcome.table_events}")
    print(f"measurement exception: {audit_history.MEASUREMENT_EXCEPTION_ID}")


def _settings_line() -> str:
    """Return the pinned planner settings as one line."""
    return ", ".join(f"{name}={value}" for name, value in audit_plan.PLANNER_SETTINGS)


def main(argv: Sequence[str] = ()) -> int:
    """Run one audit query lab command inside the API container."""
    parser = argparse.ArgumentParser(description="The Task 4.9 audit query tools.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="load the supplied large audit history, once")
    for name in ("explain", "benchmark"):
        command = commands.add_parser(name, help=f"{name} the trail query for one exception")
        command.add_argument("exception_id")
    commands.add_parser("compare", help="compare the sample trails with the stored baseline")
    commands.add_parser("index-size", help="print the size of every index on the audit table")
    verify = commands.add_parser("verify-run", help="poe verify's fresh-database procedure")
    verify.add_argument("--keep", action="store_true", help="keep the database afterwards")
    arguments = parser.parse_args(list(argv))
    url = _settings().database_url

    if arguments.command == "seed":
        _print_seed(asyncio.run(seed_history(url)))
        return 0
    if arguments.command == "explain":
        exception_id = _checked(arguments.exception_id)
        plan, indexes, events = asyncio.run(explain(url, exception_id))
        print(f"exception_id: {exception_id} ({events} audit events)")
        print(f"planner settings: {_settings_line()}; statistics refreshed with ANALYZE first")
        print()
        print("\n".join(plan))
        print()
        print(f"indexes on {audit_plan.AUDIT_TABLE}:")
        for name, definition in indexes:
            print(f"- {name}: {definition}")
        if not events:
            print("\nno audit events for this id: run `poe audit-seed-large` first")
            return 1
        return 0
    if arguments.command == "benchmark":
        exception_id = _checked(arguments.exception_id)
        result = asyncio.run(benchmark(url, exception_id))
        print(f"exception_id: {exception_id}")
        print(f"events returned: {result['events']}")
        print(f"runs: {result['runs']}, after 1 warm-up run")
        print(f"median_ms: {result['median_ms']:.3f}")
        print(f"fastest_ms: {result['fastest_ms']:.3f}  slowest_ms: {result['slowest_ms']:.3f}")
        if not result["events"]:
            print("\nno audit events for this id: run `poe audit-seed-large` first")
            return 1
        return 0
    if arguments.command == "compare":
        comparison = asyncio.run(compare_trails(url))
        for exception_id, findings in comparison.differences.items():
            verdict = "identical" if not findings else "DIFFERENT: " + "; ".join(findings)
            print(f"{exception_id}: {verdict}")
        if not comparison.events_read:
            print("\nno audit events for any sampled id: run `poe audit-seed-large` first")
            return 1
        if any(comparison.differences.values()):
            print("\nthe trail differs from the stored baseline for at least one exception")
            return 1
        print(f"\nidentical trail for all {comparison.exceptions} sampled exceptions")
        return 0
    if arguments.command == "index-size":
        sizes, table = asyncio.run(index_sizes(url))
        print(f"{audit_plan.AUDIT_TABLE} table: {table}")
        for name, size_bytes, size in sizes:
            print(f"- {name}: {size} ({size_bytes} bytes)")
        return 0
    report = verify_run(keep=arguments.keep)
    print(audit_plan.REPORT_MARKER + json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
