"""Composite scoring: the ranking that decides what gets composed.

The thing worth guarding here is `scorer`. It has to name what actually
produced the durability number, because the model is refused at some horizons
and a card that says "model" over a momentum number is the silent substitution
the contracts forbid.
"""
import pytest

from radar import db, features
from radar import score as score_mod
from radar.model.predict import MOMENTUM_SCORER, Durability

DAY = features.DAY


class FakeConfig:
    def __init__(self, **over):
        self.data = {
            "scoring.durability_horizon": 60,
            "scoring.weights": {"durability": 0.45, "saturation": 0.35, "relevance": 0.20},
            "scoring.saturation_thresholds": {"low_max": 200, "med_max": 2000},
        }
        self.data.update(over)

    def get(self, dotted, default=None):
        return self.data.get(dotted, default)


class FakeModel:
    """Stands in for DurabilityModel with a fixed answer."""

    def __init__(self, scores, scorer="model:[60]", model_horizons=(60,)):
        self.scores, self.scorer, self.model_horizons = scores, scorer, model_horizons

    def score(self, vector):
        return Durability(
            scores=self.scores, scorer=self.scorer, model_horizons=self.model_horizons
        )


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    connection.execute(
        "INSERT INTO runs (run_id, kind, started_ts, status) VALUES ('r', 'test', 1, 'ok')"
    )
    connection.execute(
        "INSERT INTO terms (id, term, normalized, origin, first_seen_ts, last_seen_ts) "
        "VALUES (1, 'ai voice clone', 'ai voice clone', 'seed', 1, 1)"
    )
    yield connection
    connection.close()


def seed_trends(conn, values, start_day=100):
    conn.executemany(
        "INSERT INTO signal_snapshots (term_id, source, metric, value, ts, run_id) "
        "VALUES (1, 'google_trends', 'interest', ?, ?, 'r')",
        [(float(v), (start_day + i) * DAY) for i, v in enumerate(values)],
    )
    conn.commit()


# --- growth -------------------------------------------------------------------


def test_growth_compares_the_last_week_against_the_week_before():
    series = [((100 + i) * DAY, 10.0) for i in range(7)]
    series += [((107 + i) * DAY, 20.0) for i in range(7)]
    assert score_mod.growth(series) == pytest.approx(1.0)   # doubled, +100%


def test_growth_is_a_fraction_not_a_percent():
    series = [((100 + i) * DAY, 100.0) for i in range(7)]
    series += [((107 + i) * DAY, 134.0) for i in range(7)]
    assert score_mod.growth(series) == pytest.approx(0.34)


def test_growth_is_zero_without_enough_history():
    assert score_mod.growth([]) == 0.0
    assert score_mod.growth([(100 * DAY, 5.0)]) == 0.0


def test_growth_from_a_zero_baseline_does_not_divide_by_zero():
    series = [((100 + i) * DAY, 0.0) for i in range(7)]
    series += [((107 + i) * DAY, 5.0) for i in range(7)]
    assert score_mod.growth(series) == 1.0

    flat_zero = [((100 + i) * DAY, 0.0) for i in range(14)]
    assert score_mod.growth(flat_zero) == 0.0


# --- the composite ------------------------------------------------------------


def test_composite_blends_the_three_axes_with_the_configured_weights(conn):
    seed_trends(conn, [50] * 20)
    model = FakeModel({30: 0.9, 60: 0.8, 90: 0.7})
    result = score_mod.score_term(conn, FakeConfig(), 1, model)

    # 0.45*0.8 durability + 0.35*(1-0) empty field + 0.20*0.5 neutral relevance
    assert result.composite == pytest.approx(0.45 * 0.8 + 0.35 * 1.0 + 0.20 * 0.5)
    assert result.saturation_label == "LOW"


def test_the_configured_horizon_is_the_one_that_drives_the_composite(conn):
    seed_trends(conn, [50] * 20)
    model = FakeModel({30: 1.0, 60: 0.0, 90: 1.0})
    at_60 = score_mod.score_term(conn, FakeConfig(), 1, model)
    at_30 = score_mod.score_term(
        conn, FakeConfig(**{"scoring.durability_horizon": 30}), 1, model
    )
    assert at_60.composite < at_30.composite


def test_a_crowded_field_scores_below_an_empty_one(conn):
    seed_trends(conn, [50] * 20)
    model = FakeModel({60: 0.8})
    empty = score_mod.score_term(conn, FakeConfig(), 1, model)

    conn.execute(
        "INSERT INTO saturation_snapshots (term_id, source, count, ts, run_id) "
        "VALUES (1, 'gumroad', 50000, 86400, 'r')"
    )
    conn.commit()
    crowded = score_mod.score_term(conn, FakeConfig(), 1, model)

    assert crowded.saturation_label == "HIGH"
    assert crowded.composite < empty.composite


def test_a_term_without_a_full_window_is_skipped_rather_than_guessed(conn):
    seed_trends(conn, [50] * 5)   # fewer than 14 days
    assert score_mod.score_term(conn, FakeConfig(), 1, FakeModel({60: 0.8})) is None


# --- scorer honesty -----------------------------------------------------------


def test_the_scorer_names_momentum_when_the_horizon_fell_back(conn):
    seed_trends(conn, [50] * 20)
    fell_back = FakeModel({60: 0.5}, scorer=MOMENTUM_SCORER, model_horizons=())
    result = score_mod.score_term(conn, FakeConfig(), 1, fell_back)

    assert MOMENTUM_SCORER in result.scorer
    assert any("not the model" in note for note in result.notes)


def test_the_scorer_names_the_model_when_the_horizon_earned_it(conn):
    seed_trends(conn, [50] * 20)
    earned = FakeModel({60: 0.9}, scorer="model:[60]", model_horizons=(60,))
    result = score_mod.score_term(conn, FakeConfig(), 1, earned)

    assert "model:60" in result.scorer
    assert not any("not the model" in note for note in result.notes)


def test_relevance_is_neutral_and_says_so_until_m9(conn):
    seed_trends(conn, [50] * 20)
    result = score_mod.score_term(conn, FakeConfig(), 1, FakeModel({60: 0.8}))
    assert result.relevance == score_mod.NEUTRAL_RELEVANCE
    assert score_mod.RELEVANCE_NEUTRAL_SCORER in result.scorer


def test_missing_supply_counts_are_reported_as_unmeasured_not_low(conn):
    """An empty field and an uncounted one score the same but do not mean the
    same, so the difference goes on the card."""
    seed_trends(conn, [50] * 20)
    result = score_mod.score_term(conn, FakeConfig(), 1, FakeModel({60: 0.8}))
    assert any("unmeasured" in note for note in result.notes)


# --- persistence and ordering -------------------------------------------------


def test_scores_are_written_with_their_scorer(conn):
    seed_trends(conn, [50] * 20)
    result = score_mod.score_term(conn, FakeConfig(), 1, FakeModel({30: 0.9, 60: 0.8, 90: 0.7}))
    assert score_mod.write_scores(conn, "r", [result]) == 1

    row = conn.execute("SELECT * FROM scores").fetchone()
    assert row["durability_60"] == pytest.approx(0.8)
    assert row["saturation_label"] == "LOW"
    assert row["scorer"] == result.scorer
    assert row["composite"] == pytest.approx(result.composite)


def test_score_terms_ranks_best_first(conn):
    seed_trends(conn, [50] * 20)
    conn.execute(
        "INSERT INTO terms (id, term, normalized, origin, first_seen_ts, last_seen_ts) "
        "VALUES (2, 'notion template', 'notion template', 'seed', 1, 1)"
    )
    conn.executemany(
        "INSERT INTO signal_snapshots (term_id, source, metric, value, ts, run_id) "
        "VALUES (2, 'google_trends', 'interest', ?, ?, 'r')",
        [(50.0, (100 + i) * DAY) for i in range(20)],
    )
    # Term 2 is in a crowded field, so it must rank below term 1.
    conn.execute(
        "INSERT INTO saturation_snapshots (term_id, source, count, ts, run_id) "
        "VALUES (2, 'gumroad', 90000, 86400, 'r')"
    )
    conn.commit()

    class Term:
        def __init__(self, id):
            self.id = id

    ranked = score_mod.score_terms(conn, FakeConfig(), [Term(2), Term(1)], FakeModel({60: 0.8}))
    assert [s.term_id for s in ranked] == [1, 2]
