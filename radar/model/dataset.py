"""History -> labelled windows.

The unlock behind M5: because Trends hands back years of daily history, the
future of every past window is already known. Labels are free, objective and
abundant -- no human annotation, no waiting six months.

Label rule, from docs/MODEL.md:

    label_N = 1 if mean(interest[t+N : t+N+7]) >= threshold * max(interest[t-14 : t])

A window is only usable if the horizon it is labelled for actually exists in
the data. Padding a missing future with zeros would teach the model that
everything dies.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .. import features

DAY = 86400
HORIZONS = (30, 60, 90)
LABEL_SPAN_DAYS = 7
PRIMARY_SOURCE = "google_trends"
PRIMARY_METRIC = "interest"


@dataclass
class Sample:
    features: features.FeatureVector
    labels: dict[int, int]  # horizon -> 0/1
    term_id: int
    window_end_ts: int


def _by_day(series: list[tuple[int, float]]) -> dict[int, float]:
    """Collapse to one value per day index. History chunks overlap, and a
    duplicated day would weight that day twice in every window it touches."""
    out: dict[int, float] = {}
    for ts, value in series:
        out.setdefault(ts // DAY, value)
    return out


def label_for(
    daily: dict[int, float],
    window_end_day: int,
    horizon: int,
    *,
    threshold: float,
    window_days: int = features.WINDOW_DAYS,
) -> int | None:
    """1 if still elevated at the horizon, 0 if not, None if unknowable.

    None is not a negative. A window whose future is missing from the data has
    no label, and treating it as 0 would train the model to predict death for
    everything recent.
    """
    peak = max(
        (daily[d] for d in range(window_end_day - window_days + 1, window_end_day + 1)
         if d in daily),
        default=0.0,
    )
    if peak <= 0:
        return None

    future = [
        daily[d]
        for d in range(window_end_day + horizon, window_end_day + horizon + LABEL_SPAN_DAYS)
        if d in daily
    ]
    if len(future) < LABEL_SPAN_DAYS:
        return None
    return int((sum(future) / len(future)) >= threshold * peak)


def build_samples(
    conn: sqlite3.Connection,
    *,
    threshold: float = 0.6,
    window_days: int = features.WINDOW_DAYS,
    horizons: tuple[int, ...] = HORIZONS,
    stride: int = 1,
) -> list[Sample]:
    """Every labelled window across every term with enough history."""
    term_rows = conn.execute("SELECT id FROM terms ORDER BY id").fetchall()
    samples: list[Sample] = []

    for row in term_rows:
        term_id = row["id"]
        series = features.series_for(conn, term_id, PRIMARY_SOURCE, PRIMARY_METRIC)
        if len(series) < window_days + max(horizons) + LABEL_SPAN_DAYS:
            continue
        daily = _by_day(series)
        days = sorted(daily)

        others = {
            source: features.series_for(conn, term_id, source, metric)
            for source, metric in (
                ("youtube", "view_sum"),
                ("hackernews", "post_count"),
                ("github", "stars"),
            )
        }
        others = {k: v for k, v in others.items() if v}

        first_day = days[0]
        for end_day in range(days[0] + window_days - 1, days[-1] + 1, stride):
            window = [
                (d * DAY, daily[d])
                for d in range(end_day - window_days + 1, end_day + 1)
                if d in daily
            ]
            if len(window) < window_days:
                continue

            labels: dict[int, int] = {}
            for horizon in horizons:
                label = label_for(
                    daily, end_day, horizon, threshold=threshold, window_days=window_days
                )
                if label is not None:
                    labels[horizon] = label
            if not labels:
                continue

            samples.append(
                Sample(
                    features=features.build(
                        term_id,
                        window,
                        other_sources=others,
                        days_observed=end_day - first_day + 1,
                    ),
                    labels=labels,
                    term_id=term_id,
                    window_end_ts=end_day * DAY,
                )
            )

    return samples


def temporal_split(
    samples: list[Sample],
    *,
    holdout_fraction: float = 0.2,
    embargo_days: int | None = None,
    horizons: tuple[int, ...] = HORIZONS,
) -> tuple[list[Sample], list[Sample]]:
    """Split by time, never at random, and embargo the seam.

    A random split puts a window's own neighbours -- overlapping by 13 of 14
    days -- on both sides of the cut and hands back a beautiful, worthless
    score. That is the rule docs/MODEL.md says must not be broken.

    Splitting by time alone is not enough here. A training window ending the
    day before the cut is *labelled* by what happened 30 to 97 days later,
    which is inside the test period: the training set would contain answers
    about the future it is being tested on. So every training window whose
    label horizon reaches past the cut is dropped. That is what `embargo_days`
    buys, and it costs real training rows on purpose.
    """
    if not samples:
        return [], []
    ordered = sorted(samples, key=lambda s: s.window_end_ts)
    cut_index = int(len(ordered) * (1 - holdout_fraction))
    cut_ts = (
        ordered[cut_index].window_end_ts
        if cut_index < len(ordered)
        else ordered[-1].window_end_ts
    )
    if embargo_days is None:
        embargo_days = max(horizons) + LABEL_SPAN_DAYS
    embargo = embargo_days * DAY

    train = [s for s in ordered if s.window_end_ts + embargo <= cut_ts]
    test = [s for s in ordered if s.window_end_ts >= cut_ts]
    return train, test
