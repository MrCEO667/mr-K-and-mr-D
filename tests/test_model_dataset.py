"""Labelling and splitting -- the two places a durability model quietly cheats.

`label_for` must return None, not 0, for a window whose future is missing, and
`temporal_split` must keep a training window's answer sheet out of the test
period. Both failures produce a better-looking number, which is exactly why
they are tested rather than trusted.
"""
from radar import features
from radar.model import dataset

DAY = dataset.DAY


def daily(values, start_day=0):
    return {start_day + i: float(v) for i, v in enumerate(values)}


def test_label_is_one_when_the_signal_is_still_elevated():
    # Flat at 100 forever: the future equals the peak.
    series = daily([100] * 130)
    assert dataset.label_for(series, 13, 30, threshold=0.6) == 1


def test_label_is_zero_when_the_signal_collapsed():
    series = daily([100] * 14 + [1] * 120)
    assert dataset.label_for(series, 13, 30, threshold=0.6) == 0


def test_label_sits_on_the_threshold_boundary():
    # Peak 100, future flat at 60 -> exactly 0.6 * peak, which counts as elevated.
    series = daily([100] * 14 + [60] * 120)
    assert dataset.label_for(series, 13, 30, threshold=0.6) == 1
    assert dataset.label_for(series, 13, 30, threshold=0.7) == 0


def test_a_missing_future_is_unlabelled_rather_than_negative():
    # History stops right after the window. The +30d outcome is unknown.
    series = daily([100] * 20)
    assert dataset.label_for(series, 13, 30, threshold=0.6) is None


def test_a_partial_future_window_is_also_unlabelled():
    # Only 3 of the 7 label days exist; averaging them would invent an answer.
    series = daily([100] * 14 + [50] * 29 + [50] * 3)
    assert len(series) < 13 + 30 + dataset.LABEL_SPAN_DAYS
    assert dataset.label_for(series, 13, 30, threshold=0.6) is None


def test_an_all_zero_window_has_no_peak_to_measure_against():
    series = daily([0] * 130)
    assert dataset.label_for(series, 13, 30, threshold=0.6) is None


def sample(term_id, day, labels=None):
    window = [((day - 13 + i) * DAY, float(i)) for i in range(14)]
    return dataset.Sample(
        features=features.build(term_id, window),
        labels=labels if labels is not None else {30: 1, 60: 1, 90: 1},
        term_id=term_id,
        window_end_ts=day * DAY,
    )


def test_split_is_temporal_not_random():
    samples = [sample(1, day) for day in range(100, 300)]
    train, test = dataset.temporal_split(samples, holdout_fraction=0.2)
    assert train and test
    assert max(s.window_end_ts for s in train) < min(s.window_end_ts for s in test)


def test_training_windows_whose_labels_reach_into_the_test_period_are_dropped():
    """The embargo. A window ending the day before the cut is labelled by what
    happened up to 97 days later -- inside the test period. Keeping it would
    put the test set's answers in the training set."""
    samples = [sample(1, day) for day in range(100, 300)]
    train, test = dataset.temporal_split(samples, holdout_fraction=0.2)
    cut = min(s.window_end_ts for s in test)

    embargo = (max(dataset.HORIZONS) + dataset.LABEL_SPAN_DAYS) * DAY
    assert all(s.window_end_ts + embargo <= cut for s in train)

    # And the embargo actually costs rows -- a no-op embargo is not an embargo.
    naive_train = [s for s in samples if s.window_end_ts < cut]
    assert len(train) < len(naive_train)


def test_empty_input_splits_into_nothing_rather_than_raising():
    assert dataset.temporal_split([]) == ([], [])
