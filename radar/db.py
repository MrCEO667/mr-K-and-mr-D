"""SQLite connection, schema init, and the run_id every write hangs off.

SQLite is the only shared state in this system (ARCHITECTURE rule 3), so this
module owns the connection and the runs table and nothing else.

--dry-run is enforced here rather than trusted to each caller: the run opens a
transaction that is always rolled back, so a dry run physically cannot leave a
row behind, however a downstream module misbehaves.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import log

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "schema.sql"

VALID_KINDS = ("watchlist", "sweep", "saturation", "train")
VALID_STATUSES = ("running", "ok", "degraded", "failed")


def connect(db_path: Path | str, *, init: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if init:
        init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection, schema_path: Path | None = None) -> None:
    """Apply schema.sql. Every statement is CREATE ... IF NOT EXISTS, so this
    is safe on an existing database and is the closest thing to a migration
    the project has until something actually needs altering."""
    sql = (schema_path or SCHEMA_PATH).read_text(encoding="utf-8")
    conn.executescript(sql)


def new_run_id() -> str:
    return str(uuid.uuid4())


def now() -> int:
    return int(time.time())


@contextmanager
def run(
    conn: sqlite3.Connection,
    kind: str,
    *,
    dry_run: bool = False,
) -> Iterator[str]:
    """Open a run, yield its run_id, and close it with an honest status.

    On an exception the run is marked failed and the error recorded before the
    exception continues -- a crashed run must never be left saying 'running'.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown run kind {kind!r}; expected one of {VALID_KINDS}")

    run_id = new_run_id()
    logger = log.get(__name__, run_id=run_id, kind=kind, dry_run=dry_run)

    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO runs (run_id, kind, started_ts, status) VALUES (?, ?, ?, 'running')",
        (run_id, kind, now()),
    )
    logger.info("run started")
    try:
        yield run_id
    except Exception as exc:
        conn.execute(
            "UPDATE runs SET finished_ts = ?, status = 'failed', notes = ? WHERE run_id = ?",
            (now(), f"{type(exc).__name__}: {exc}", run_id),
        )
        conn.execute("ROLLBACK" if dry_run else "COMMIT")
        logger.exception("run failed")
        raise
    else:
        # Only a run that was never degraded finishes 'ok'. Overwriting the
        # label here would erase exactly the signal the failure policy exists
        # to preserve.
        conn.execute(
            "UPDATE runs SET finished_ts = ?, "
            "status = CASE WHEN status = 'running' THEN 'ok' ELSE status END "
            "WHERE run_id = ?",
            (now(), run_id),
        )
        if dry_run:
            conn.execute("ROLLBACK")
            logger.info("run finished, rolled back (dry run wrote nothing)")
        else:
            conn.execute("COMMIT")
            logger.info("run finished")


def mark_degraded(conn: sqlite3.Connection, run_id: str, note: str) -> None:
    """Degrade, never crash (ARCHITECTURE failure policy). A degraded run still
    delivers; it just says so."""
    conn.execute(
        "UPDATE runs SET status = 'degraded', notes = COALESCE(notes || ' | ', '') || ? "
        "WHERE run_id = ?",
        (note, run_id),
    )


def record_source_health(
    conn: sqlite3.Connection,
    run_id: str,
    source: str,
    *,
    status: str,
    latency_ms: int | None = None,
    error_count: int = 0,
    message: str | None = None,
) -> None:
    """Every degradation is visible in the logs and on /why, so it has to be
    written down at the moment it happens."""
    if status not in ("ok", "degraded", "down"):
        raise ValueError(f"unknown source status {status!r}")
    conn.execute(
        "INSERT INTO source_health "
        "(source, run_id, status, latency_ms, error_count, message, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, run_id, status, latency_ms, error_count, message, now()),
    )


def write_readings(conn: sqlite3.Connection, run_id: str, readings) -> int:
    """Append signal_snapshots rows. Returns how many were actually new.

    This table is append-only: never UPDATE, never DELETE. Two points are the
    minimum for velocity and many are needed for durability, which is the
    lesson Retrend paid for by storing a single snapshot and being unable to
    compute anything at all.

    A re-run covering the same window is normal -- Trends returns history on
    every call -- so the unique index absorbs repeats and the count reflects
    genuinely new observations rather than work done.
    """
    written = 0
    for r in readings:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO signal_snapshots "
            "(term_id, source, metric, value, ts, run_id) VALUES (?, ?, ?, ?, ?, ?)",
            (r.term_id, r.source, r.metric, r.value, r.ts, run_id),
        )
        written += cursor.rowcount or 0
    return written


def write_evidence(conn: sqlite3.Connection, run_id: str, items) -> int:
    """Evidence rows are what /why shows the operator, so they carry the run
    that produced them and stay traceable back to a source URL."""
    written = 0
    for item in items:
        conn.execute(
            "INSERT INTO evidence "
            "(term_id, source, url, title, snippet, metric_json, ts, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.term_id,
                item.source,
                item.url,
                item.title,
                item.snippet,
                json.dumps(item.metric_json) if item.metric_json is not None else None,
                now(),
                run_id,
            ),
        )
        written += 1
    return written


def series(conn: sqlite3.Connection, term: str, *, source: str | None = None):
    """The time series for one term, oldest first. Used by radar.report."""
    sql = (
        "SELECT s.ts, s.source, s.metric, s.value, s.run_id "
        "FROM signal_snapshots s JOIN terms t ON t.id = s.term_id "
        "WHERE t.term = ? OR t.normalized = ?"
    )
    params: list[object] = [term, term.lower().strip()]
    if source:
        sql += " AND s.source = ?"
        params.append(source)
    return conn.execute(sql + " ORDER BY s.ts", params).fetchall()
