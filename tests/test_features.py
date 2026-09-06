"""Window -> FeatureVector. The contract training and scoring share.

The single rule worth a test here is that `as_row()` follows FEATURE_NAMES.
Everything else is arithmetic; that one is a silent retraining of meaning into
the wrong column if it ever drifts.
"""
import math

import pytest

from radar import features

DAY = features.DAY


def window(values, start_day=0):
    return [((start_day + i) * DAY, float(v)) for i, v in enumerate(values)]


def test_as_row_follows_feature_names_exactly():
    vector = features.build(1, window(range(14)), days_observed=14)
    row = vector.as_row()
    assert len(row) == len(features.FEATURE_NAMES)
    for name, value in zip(features.FEATURE_NAMES, row, strict=True):
        assert value == float(getattr(vector, name))


def test_rising_and_falling_windows_have_opposite_slope():
    rising = features.build(1, window(range(14)), days_observed=14)
    falling = features.build(1, window(range(13, -1, -1)), days_observed=14)
    assert rising.slope > 0
    assert falling.slope < 0
    assert math.isclose(rising.slope, -falling.slope, rel_tol=1e-9)


def test_peak_relative_is_one_at_the_peak_and_low_after_a_crash():
    at_peak = features.build(1, window([1] * 13 + [100]), days_observed=14)
    crashed = features.build(1, window([100] + [1] * 13), days_observed=14)
    assert at_peak.peak_relative == 1.0
    assert crashed.peak_relative < 0.1


def test_flat_window_has_no_volatility_and_no_slope():
    flat = features.build(1, window([50] * 14), days_observed=14)
    assert flat.slope == 0
    assert flat.volatility == 0


def test_breadth_counts_the_primary_source_plus_sources_that_saw_it():
    alone = features.build(1, window(range(14)), days_observed=14)
    assert alone.source_breadth == 1

    seen = features.build(
        1,
        window(range(14)),
        days_observed=14,
        other_sources={
            "youtube": window(range(14)), "hackernews": [], "github": window(range(14))
        },
    )
    # Empty series means the source did not see the term, so it is not breadth.
    assert seen.source_breadth == 3


def test_correlation_is_signed_and_ignores_series_too_short_to_measure():
    together = features.build(
        1, window(range(14)), days_observed=14, other_sources={"youtube": window(range(14))}
    )
    opposed = features.build(
        1, window(range(14)), days_observed=14,
        other_sources={"youtube": window(range(13, -1, -1))},
    )
    assert together.source_correlation > 0.9
    assert opposed.source_correlation < -0.9

    too_short = features.build(
        1, window(range(14)), days_observed=14, other_sources={"youtube": window([1, 2])}
    )
    assert too_short.source_correlation == 0.0


def test_series_for_reads_only_the_requested_source_and_metric(tmp_path):
    from radar import db

    conn = db.connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO runs (run_id, kind, started_ts, status) VALUES ('r', 'test', 1, 'ok')"
    )
    conn.execute(
        "INSERT INTO terms (id, term, normalized, origin, first_seen_ts, last_seen_ts) "
        "VALUES (1, 'x', 'x', 'seed', 1, 1)"
    )
    conn.executemany(
        "INSERT INTO signal_snapshots (term_id, source, metric, value, ts, run_id) "
        "VALUES (?, ?, ?, ?, ?, 'r')",
        [
            (1, "google_trends", "interest", 10.0, 2 * DAY),
            (1, "google_trends", "interest", 20.0, 1 * DAY),
            (1, "youtube", "view_sum", 999.0, 1 * DAY),
        ],
    )
    series = features.series_for(conn, 1, "google_trends", "interest")
    assert series == [(1 * DAY, 20.0), (2 * DAY, 10.0)]  # ordered by ts
    conn.close()


# --- regressions: cross-source features were computed against the wrong slice


def test_correlation_uses_the_contemporaneous_days_not_the_start_of_history():
    """The other source may arrive as its full history. build() must align it
    to the window itself.

    This was wrong: _correlation truncated to min(len(a), len(b)), so a 14-day
    window was compared against the *first* 14 days of a two-year series. A
    source rising in perfect lockstep with the window scored -1.0.
    """
    win = window(range(14), start_day=250)

    youtube = window([20 - i for i in range(14)], start_day=100)  # falls, long before
    youtube += window([5] * 14, start_day=200)                    # flat filler
    youtube += window(range(14), start_day=250)                   # rises WITH the window

    vector = features.build(1, win, days_observed=200, other_sources={"youtube": youtube})
    assert vector.source_correlation == pytest.approx(1.0)


def test_slicing_happens_inside_build_so_the_caller_cannot_get_it_wrong():
    win = window(range(14), start_day=250)
    aligned = window(range(14), start_day=250)
    full = window([20 - i for i in range(14)], start_day=100) + aligned

    from_full = features.build(1, win, days_observed=200, other_sources={"youtube": full})
    from_slice = features.build(1, win, days_observed=200, other_sources={"youtube": aligned})
    assert from_full.source_correlation == pytest.approx(from_slice.source_correlation)


def test_breadth_counts_only_sources_that_saw_the_term_during_this_window():
    """Breadth was constant per term because every window got the source's
    whole history, so a feature meant to vary carried no information."""
    win = window(range(14), start_day=250)

    elsewhere = window(range(5), start_day=10)          # data, but nowhere near
    during = window(range(14), start_day=250)

    assert features.build(1, win, days_observed=200,
                          other_sources={"youtube": elsewhere}).source_breadth == 1
    assert features.build(1, win, days_observed=200,
                          other_sources={"youtube": during}).source_breadth == 2
    assert features.build(1, win, days_observed=200,
                          other_sources={"youtube": during, "github": elsewhere},
                          ).source_breadth == 2


def test_partial_overlap_still_correlates_on_the_shared_days():
    win = window(range(14), start_day=250)
    partial = window(range(6), start_day=250)  # only the first six days overlap
    vector = features.build(1, win, days_observed=200, other_sources={"youtube": partial})
    assert vector.source_breadth == 2
    assert vector.source_correlation == pytest.approx(1.0)


def test_days_observed_is_required_so_training_and_scoring_cannot_diverge():
    with pytest.raises(TypeError):
        features.build(1, window(range(14)))
