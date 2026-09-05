"""M1's done-condition: seed terms in, signal_snapshots rows out, and two runs
an hour apart produce two distinct rows per term.
"""
import pandas as pd
import yaml

from radar import db, discover, report, runner
from radar.collectors.base import RateLimiter
from radar.collectors.trends import ANCHOR_TERM, TrendsCollector

SEEDS = {"categories": {"ai_tools": ["ai voice clone"]}, "exclude_patterns": []}


class FakeTrends:
    """Returns a frame whose timestamps shift, as a real hour-later call would."""

    def __init__(self, day):
        self.day = day
        self._kw = None

    def build_payload(self, kw_list, timeframe=None, geo=None):
        self._kw = kw_list

    def interest_over_time(self):
        idx = pd.date_range(f"2026-01-{self.day:02d}", periods=2, freq="h", tz="UTC")
        values = {k: [50, 50] if k == ANCHOR_TERM else [25, 30] for k in self._kw}
        return pd.DataFrame(values, index=idx)


def collect_once(conn, day, run_kind="sweep"):
    terms = discover.seed_terms(conn)
    collector = TrendsCollector(
        client=FakeTrends(day), rate_limiter=RateLimiter(min_interval_s=0, sleep=lambda s: None)
    )
    with db.run(conn, run_kind) as run_id:
        result = collector.collect(terms, run_id)
        return db.write_readings(conn, run_id, result.readings)


def setup_seeds(tmp_path, monkeypatch):
    path = tmp_path / "seeds.yaml"
    path.write_text(yaml.safe_dump(SEEDS), encoding="utf-8")
    monkeypatch.setattr(discover, "DEFAULT_SEEDS", path)


def test_two_runs_produce_distinct_snapshot_rows(tmp_path, monkeypatch):
    setup_seeds(tmp_path, monkeypatch)
    conn = db.connect(tmp_path / "radar.db")

    first = collect_once(conn, day=1)
    second = collect_once(conn, day=2)

    assert first == 2
    assert second == 2
    rows = conn.execute("SELECT DISTINCT run_id FROM signal_snapshots").fetchall()
    assert len(rows) == 2


def test_recollecting_the_same_window_does_not_duplicate(tmp_path, monkeypatch):
    setup_seeds(tmp_path, monkeypatch)
    conn = db.connect(tmp_path / "radar.db")
    collect_once(conn, day=1)
    # Trends returns history on every call, so overlap is the normal case.
    assert collect_once(conn, day=1) == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_snapshots").fetchone()[0] == 2


def test_report_prints_the_series(tmp_path, monkeypatch):
    setup_seeds(tmp_path, monkeypatch)
    conn = db.connect(tmp_path / "radar.db")
    collect_once(conn, day=1)
    rows = db.series(conn, "ai voice clone")
    out = report.format_series(rows, "ai voice clone")
    assert "2 snapshots" in out
    assert "0.5" in out or "0.500" in out


def test_a_dead_source_degrades_the_run_instead_of_killing_it(tmp_path, monkeypatch):
    setup_seeds(tmp_path, monkeypatch)
    conn = db.connect(tmp_path / "radar.db")

    class Dead:
        source = "google_trends"

        def collect(self, terms, run_id):
            from radar.collectors.base import SourceUnavailable

            raise SourceUnavailable("google said no")

        def health(self):  # pragma: no cover - never reached
            raise AssertionError

    terms = discover.seed_terms(conn)
    with db.run(conn, "sweep") as run_id:
        runner.run_collectors(conn, [Dead()], terms, run_id)

    run_row = conn.execute("SELECT status, notes FROM runs").fetchone()
    assert run_row["status"] == "degraded"
    assert "unavailable" in run_row["notes"]
    health = conn.execute("SELECT status FROM source_health").fetchone()
    assert health["status"] == "down"
