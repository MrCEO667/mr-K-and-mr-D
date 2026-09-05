"""The schema must apply cleanly and keep the operator columns.

decisions is the M9 training set. If actor_tg_id is ever dropped, two
operators' labels merge into one indistinguishable set -- see DECISIONS 17.
"""
import pathlib
import sqlite3

SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "schema" / "schema.sql"


def columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    return db


def test_schema_applies():
    tables = {
        row[0]
        for row in fresh_db().execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"terms", "signal_snapshots", "decisions", "outcomes", "alerts"} <= tables


def test_decisions_record_the_operator():
    assert {"actor_tg_id", "actor_name"} <= columns(fresh_db(), "decisions")


def test_outcomes_record_the_operator():
    assert {"actor_tg_id", "actor_name"} <= columns(fresh_db(), "outcomes")
