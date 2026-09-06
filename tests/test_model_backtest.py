"""The honesty clause, as code.

PROMPT.md M5: "Shipping a model that loses to a one-line heuristic and not
saying so is the single worst outcome of this project." These tests are what
stop that from happening quietly -- a losing model must be reported as losing
and must not be loaded.
"""
import json

import pytest

from radar import features
from radar.model import backtest, dataset, predict


class FakeModel:
    """predict_proba driven by a fixed score per row, keyed on slope."""

    def __init__(self, scores):
        self.scores = scores

    def predict_proba(self, rows):
        # numpy, because backtest.run slices the real sklearn output as [:, 1].
        import numpy

        return numpy.array([[1 - self.scores[row[0]], self.scores[row[0]]] for row in rows])


def sample(slope, label, day=0):
    vector = features.FeatureVector(
        term_id=1, window_end_ts=day * dataset.DAY, slope=slope, acceleration=0.0,
        volatility=0.0, seasonality_amp=0.0, peak_relative=1.0, days_observed=14,
        source_breadth=1, source_correlation=0.0, magnitude_bucket=1,
    )
    return dataset.Sample(
        features=vector, labels={30: label}, term_id=1, window_end_ts=day * dataset.DAY
    )


def test_precision_at_k_counts_only_the_top_k():
    assert backtest.precision_at_k([1, 1, 0, 0], 2) == 1.0
    assert backtest.precision_at_k([0, 0, 1, 1], 2) == 0.0
    assert backtest.precision_at_k([1, 0], 10) == 0.5  # fewer than k available
    assert backtest.precision_at_k([], 10) == 0.0


def test_momentum_baseline_ranks_by_slope_alone():
    # The high-slope half is the elevated half, so slope alone is a perfect ranker.
    test = [sample(slope, 1, day=i) for i, slope in enumerate([9.0, 8.0, 7.0])]
    test += [sample(slope, 0, day=10 + i) for i, slope in enumerate([1.0, 2.0, 3.0])]
    result = backtest.run(test, None, 30, k=3)
    assert result.momentum_precision == 1.0
    assert result.model_precision is None


def test_a_model_that_loses_to_momentum_is_reported_as_losing():
    test = [sample(slope, 1, day=i) for i, slope in enumerate([9.0, 8.0, 7.0])]
    test += [sample(slope, 0, day=10 + i) for i, slope in enumerate([1.0, 2.0, 3.0])]
    # Ranks the dead ones first: exactly backwards.
    losing = FakeModel({9.0: 0.1, 8.0: 0.1, 7.0: 0.1, 1.0: 0.9, 2.0: 0.9, 3.0: 0.9})

    result = backtest.run(test, losing, 30, k=3)
    assert result.model_precision == 0.0
    assert result.momentum_precision == 1.0
    assert not result.beats_momentum
    assert not result.use_model

    text = backtest.verdict([result])
    assert "No horizon earned the model" in text
    assert "momentum_fallback" in text
    assert not backtest.should_use_model([result], 30)


def test_a_model_that_wins_says_so():
    test = [
        sample(1.0, 1, day=0), sample(9.0, 0, day=1),
        sample(2.0, 1, day=2), sample(8.0, 0, day=3),
    ]
    winning = FakeModel({1.0: 0.9, 2.0: 0.8, 9.0: 0.1, 8.0: 0.1})
    result = backtest.run(test, winning, 30, k=2, auc=0.80)
    assert result.model_precision == 1.0
    assert result.momentum_precision == 0.0
    assert result.use_model
    assert "earned every horizon tested" in backtest.verdict([result])


def test_a_head_that_wins_on_ten_rows_but_cannot_rank_the_rest_is_refused():
    """The 90d case from the real backtest: precision@10 looked excellent and
    AUC over the full test set was 0.53. A lucky top ten is not a ranker."""
    test = [
        sample(1.0, 1, day=0), sample(9.0, 0, day=1),
        sample(2.0, 1, day=2), sample(8.0, 0, day=3),
    ]
    lucky = FakeModel({1.0: 0.9, 2.0: 0.8, 9.0: 0.1, 8.0: 0.1})
    result = backtest.run(test, lucky, 30, k=2, auc=0.53)
    assert result.beats_momentum and result.beats_random
    assert not result.clears_auc_floor
    assert not result.use_model


def test_an_unmeasured_auc_does_not_waive_the_floor():
    result = backtest.BacktestResult(30, 100, 10, 0.5, 0.9, 0.4, 0.5, auc=None)
    assert result.beats_momentum and result.beats_random
    assert not result.use_model


def test_a_tie_on_the_headline_metric_is_broken_at_a_wider_k():
    """precision@10 saturates: with few terms both rankers hit 1.00 and the
    metric has run out of resolution. The tiebreak is consulted only then."""
    tied_and_better = backtest.BacktestResult(
        30, 3084, 10, 0.58, 1.00, 1.00, 0.59,
        tiebreak_k=100, wide_model_precision=0.98, wide_momentum_precision=0.85, auc=0.73,
    )
    assert tied_and_better.ties_momentum
    assert not tied_and_better.beats_momentum
    assert tied_and_better.use_model

    tied_and_worse = backtest.BacktestResult(
        30, 3084, 10, 0.58, 1.00, 1.00, 0.59,
        tiebreak_k=100, wide_model_precision=0.70, wide_momentum_precision=0.85, auc=0.73,
    )
    assert not tied_and_worse.use_model


def test_the_tiebreak_cannot_rescue_a_head_that_actually_lost():
    # Losing the headline metric is a loss; a wider k does not get a vote.
    lost = backtest.BacktestResult(
        60, 2345, 10, 0.49, 0.80, 0.90, 0.47,
        tiebreak_k=100, wide_model_precision=0.99, wide_momentum_precision=0.10, auc=0.90,
    )
    assert not lost.beats_momentum
    assert not lost.use_model


def test_random_baseline_lands_near_the_base_rate():
    test = [sample(float(i), i % 2, day=i) for i in range(100)]
    result = backtest.run(test, None, 30, k=10, random_trials=500)
    assert result.base_rate == pytest.approx(0.5)
    assert result.random_precision == pytest.approx(0.5, abs=0.08)


def test_no_labelled_windows_produces_a_plain_statement_not_a_crash():
    assert "No backtest was possible" in backtest.verdict([])
    result = backtest.run([], None, 30, k=10)
    assert result.n == 0 and result.model_precision is None


def test_allowed_horizons_come_from_the_recorded_verdict():
    block = backtest.to_metadata(
        [
            backtest.BacktestResult(30, 100, 10, 0.5, 0.9, 0.4, 0.5, auc=0.7),   # wins
            backtest.BacktestResult(60, 100, 10, 0.5, 0.3, 0.8, 0.5, auc=0.7),   # loses to momentum
            backtest.BacktestResult(90, 100, 10, 0.5, 0.55, 0.4, 0.6, auc=0.7),  # loses to random
        ]
    )
    assert backtest.allowed_from_metadata({"backtest": block}) == [30]


def test_a_model_with_no_recorded_backtest_is_unknown_not_approved():
    assert backtest.allowed_from_metadata({}) is None
    assert backtest.allowed_from_metadata({"backtest": {}}) is None


def test_loader_refuses_an_unbacktested_model_and_falls_back_loudly(tmp_path, caplog):
    import joblib

    joblib.dump(FakeModel({0.5: 0.9}), tmp_path / "durability_30.pkl")
    (tmp_path / "metadata.json").write_text(json.dumps({"features": []}), encoding="utf-8")

    model = predict.DurabilityModel(tmp_path).load()
    assert not model.available
    assert model.loaded_horizons == []

    vector = sample(0.5, 1).features
    assert model.score(vector).scorer == predict.MOMENTUM_SCORER


def test_loader_keeps_only_the_horizons_that_won(tmp_path):
    import joblib

    for horizon in (30, 60, 90):
        joblib.dump(FakeModel({0.5: 0.9}), tmp_path / f"durability_{horizon}.pkl")
    block = backtest.to_metadata(
        [
            backtest.BacktestResult(30, 100, 10, 0.5, 0.9, 0.4, 0.5, auc=0.7),  # wins
            backtest.BacktestResult(60, 100, 10, 0.5, 0.3, 0.8, 0.5, auc=0.7),  # loses
            backtest.BacktestResult(90, 100, 10, 0.5, 0.2, 0.8, 0.5, auc=0.7),  # loses
        ]
    )
    (tmp_path / "metadata.json").write_text(json.dumps({"backtest": block}), encoding="utf-8")

    model = predict.DurabilityModel(tmp_path).load()
    assert model.loaded_horizons == [30]

    # The horizons that lost still get a number -- from momentum, and the
    # scorer name says the model was involved only where it earned it.
    scored = model.score(sample(0.5, 1).features)
    assert set(scored.scores) == set(dataset.HORIZONS)
    assert scored.scorer == "model:[30]"


def test_each_horizon_reports_what_actually_produced_its_number(tmp_path):
    """A bundle scored partly by the model and partly by momentum must not let
    a momentum-filled horizon inherit the model's name."""
    import joblib

    joblib.dump(FakeModel({0.5: 0.9}), tmp_path / "durability_30.pkl")
    joblib.dump(FakeModel({0.5: 0.9}), tmp_path / "durability_60.pkl")
    block = backtest.to_metadata(
        [
            backtest.BacktestResult(30, 100, 10, 0.5, 0.9, 0.4, 0.5, auc=0.7),  # wins
            backtest.BacktestResult(60, 100, 10, 0.5, 0.3, 0.8, 0.5, auc=0.7),  # loses
        ]
    )
    (tmp_path / "metadata.json").write_text(json.dumps({"backtest": block}), encoding="utf-8")

    scored = predict.DurabilityModel(tmp_path).load().score(sample(0.5, 1).features)
    assert scored.scorer_for(30) == "model:30"
    assert scored.scorer_for(60) == predict.MOMENTUM_SCORER
    assert scored.scorer_for(90) == predict.MOMENTUM_SCORER


def test_a_pure_fallback_bundle_names_momentum_for_every_horizon(tmp_path):
    scored = predict.DurabilityModel(tmp_path).load().score(sample(0.5, 1).features)
    assert scored.scorer == predict.MOMENTUM_SCORER
    assert all(scored.scorer_for(h) == predict.MOMENTUM_SCORER for h in dataset.HORIZONS)


def test_the_verdict_describes_what_the_loader_actually_does():
    """The verdict text is what PROMPT.md requires be pasted into MODEL.md, and
    the loader gates on use_model. Reporting beats_momentum instead announced a
    winner the loader then refused, and stayed silent about one it accepted."""
    # Beat momentum on the headline metric, but cannot rank the rest.
    lucky = backtest.BacktestResult(
        90, 1595, 10, 0.38, 0.90, 0.30, 0.36,
        tiebreak_k=100, wide_model_precision=0.64, wide_momentum_precision=0.64, auc=0.51,
    )
    # Tied the headline metric, won the tiebreak, ranks well overall.
    tied = backtest.BacktestResult(
        30, 3084, 10, 0.58, 1.00, 1.00, 0.59,
        tiebreak_k=100, wide_model_precision=0.98, wide_momentum_precision=0.85, auc=0.75,
    )
    text = backtest.verdict([tied, lucky])

    assert lucky.beats_momentum and not lucky.use_model
    assert tied.use_model and not tied.beats_momentum

    assert "used at +30d" in text
    assert "refused at +90d" in text
    assert "is still refused" in text          # explains the lucky top ten
    assert "won the precision@100 tiebreak" in text


def test_the_tiebreak_refuses_to_rule_when_k_covers_the_whole_test_set():
    """precision_at_k divides by whatever exists, so at k >= n both rankers
    score every label and tie by construction -- the tiebreak would be dead
    exactly on the small test sets it was introduced to rescue."""
    small = backtest.BacktestResult(
        30, 40, 10, 0.5, 1.00, 1.00, 0.5,
        tiebreak_k=100, wide_model_precision=0.5, wide_momentum_precision=0.5, auc=0.9,
    )
    assert not small.tiebreak_is_measurable
    assert not small.wins_tiebreak
    assert not small.use_model

    big = backtest.BacktestResult(
        30, 3084, 10, 0.5, 1.00, 1.00, 0.5,
        tiebreak_k=100, wide_model_precision=0.98, wide_momentum_precision=0.85, auc=0.9,
    )
    assert big.tiebreak_is_measurable
    assert big.use_model
