"""Entry point behaviour: flags, refusals, and what a run leaves behind."""
import sqlite3

import pytest
import yaml

from radar import runner

CONFIG = {
    "db": {"path": "data/radar.db"},
    "telegram": {"operators": [{"id": 111, "name": "Mr K"}, {"id": 222, "name": "Mr D"}]},
    # No implemented source is enabled: this suite exercises the entry point,
    # not the collectors, and must never reach the network.
    "sources": {"tikwm": {"enabled": True}},
}


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    data = {**CONFIG, "db": {"path": str(tmp_path / "radar.db")}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return path


def rows(tmp_path, table):
    conn = sqlite3.connect(tmp_path / "radar.db")
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def test_a_single_pass_records_one_run(cfg_path, tmp_path):
    assert runner.main(["--once", "--config", str(cfg_path)]) == 0
    assert len(rows(tmp_path, "runs")) == 1


def test_dry_run_writes_nothing(cfg_path, tmp_path):
    assert runner.main(["--once", "--dry-run", "--config", str(cfg_path)]) == 0
    assert rows(tmp_path, "runs") == []


def test_dry_run_writes_no_scores_either(cfg_path, tmp_path):
    """Regression. `db.run` owns one transaction for the whole run; a stage
    that commits its own writes ends it early, so the run's ROLLBACK fails and
    --dry-run -- which promises to write nothing -- has already written."""
    assert runner.main(["--once", "--dry-run", "--config", str(cfg_path)]) == 0
    for table in ("runs", "scores", "opportunities", "signal_snapshots", "terms"):
        assert rows(tmp_path, table) == [], f"{table} was written during a dry run"


def test_without_once_it_refuses_rather_than_looping(cfg_path):
    assert runner.main(["--config", str(cfg_path)]) == 2


def test_bad_config_exits_two_and_does_not_crash(tmp_path):
    assert runner.main(["--once", "--config", str(tmp_path / "absent.yaml")]) == 2


def test_enabled_source_without_credentials_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    data = {
        **CONFIG,
        "db": {"path": str(tmp_path / "radar.db")},
        "sources": {"youtube": {"enabled": True}},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    # Silent zero-row collection is the predecessor's signature failure.
    assert runner.main(["--once", "--config", str(path)]) == 1


def test_run_kind_is_recorded(cfg_path, tmp_path):
    runner.main(["--once", "--kind", "watchlist", "--config", str(cfg_path)])
    assert rows(tmp_path, "runs")[0][1] == "watchlist"
