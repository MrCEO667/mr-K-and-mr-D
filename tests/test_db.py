"""Run lifecycle, and the guarantee that --dry-run writes nothing."""
import pytest

from radar import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def runs(conn):
    return conn.execute("SELECT * FROM runs").fetchall()


def test_a_completed_run_is_recorded_ok(conn):
    with db.run(conn, "sweep") as run_id:
        assert run_id
    row = runs(conn)[0]
    assert row["status"] == "ok"
    assert row["finished_ts"] is not None


def test_a_crashed_run_is_never_left_running(conn):
    with pytest.raises(ValueError, match="boom"), db.run(conn, "sweep"):
        raise ValueError("boom")
    row = runs(conn)[0]
    assert row["status"] == "failed"
    assert "boom" in row["notes"]


def test_dry_run_leaves_no_trace(conn):
    with db.run(conn, "sweep", dry_run=True) as run_id:
        conn.execute(
            "INSERT INTO terms (term, normalized, origin, first_seen_ts, last_seen_ts) "
            "VALUES ('ai voice clone', 'ai voice clone', 'seed', 1, 1)"
        )
        assert run_id
    # The run row and anything written during it are both rolled back.
    assert runs(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 0


def test_a_real_run_does_persist(conn):
    with db.run(conn, "sweep"):
        conn.execute(
            "INSERT INTO terms (term, normalized, origin, first_seen_ts, last_seen_ts) "
            "VALUES ('ai voice clone', 'ai voice clone', 'seed', 1, 1)"
        )
    assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 1


def test_unknown_run_kind_is_refused(conn):
    with pytest.raises(ValueError, match="unknown run kind"), db.run(conn, "nonsense"):
        pass


def test_degraded_runs_say_so(conn):
    with db.run(conn, "sweep") as run_id:
        db.mark_degraded(conn, run_id, "tikwm timed out")
    row = runs(conn)[0]
    assert row["status"] == "degraded"
    assert "tikwm" in row["notes"]


def test_source_health_is_written_against_a_run(conn):
    with db.run(conn, "sweep") as run_id:
        db.record_source_health(conn, run_id, "tikwm", status="down", message="503")
    row = conn.execute("SELECT * FROM source_health").fetchone()
    assert row["source"] == "tikwm"
    assert row["status"] == "down"
    assert row["run_id"] == run_id


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    db.connect(path).close()
    db.connect(path).close()  # must not raise on an existing database
