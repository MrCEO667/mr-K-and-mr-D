"""Window -> FeatureVector. Frozen contract, section 2.

**Used identically by training and scoring. Never fork this function.** A
feature computed one way at train time and another at predict time is a silent
bug that no test of either side alone will catch, so both sides call exactly
this code.

The window is 14 days of anchored Trends values plus whatever other sources saw
the term, which is where `source_breadth` and `source_correlation` come from:
something rising on Trends *and* YouTube *and* Hacker News behaves differently
from something rising on one.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass

WINDOW_DAYS = 14
DAY = 86400

FEATURE_NAMES = [
    "slope",
    "acceleration",
    "volatility",
    "seasonality_amp",
    "peak_relative",
    "days_observed",
    "source_breadth",
    "source_correlation",
    "magnitude_bucket",
]


@dataclass
class FeatureVector:
    term_id: int
    window_end_ts: int
    slope: float
    acceleration: float
    volatility: float
    seasonality_amp: float
    peak_relative: float
    days_observed: int
    source_breadth: int
    source_correlation: float
    magnitude_bucket: int

    def as_row(self) -> list[float]:
        """Ordered exactly as FEATURE_NAMES. The model sees this order and no
        other; a reordering here silently retrains meaning into the wrong
        column."""
        data = asdict(self)
        return [float(data[name]) for name in FEATURE_NAMES]


def _ols_slope(values: list[float]) -> float:
    """Least-squares slope against index. Zero for fewer than two points."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    numerator = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    return numerator / denominator if denominator else 0.0


def _acceleration(values: list[float]) -> float:
    """Difference between the second half's slope and the first half's.

    A second difference on noisy daily data is mostly noise; comparing the two
    halves answers the same question -- is the rise steepening -- with far more
    of the window behind each number.
    """
    if len(values) < 4:
        return 0.0
    half = len(values) // 2
    return _ols_slope(values[half:]) - _ols_slope(values[:half])


def _volatility(values: list[float]) -> float:
    """Coefficient of variation. Scale-free, which matters because anchored
    Trends values differ by orders of magnitude between terms."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance) / mean


def _seasonality_amp(points: list[tuple[int, float]]) -> float:
    """Weekday effect: how far the strongest weekday sits from the weakest,
    relative to the mean. Search demand for anything work-shaped has one."""
    if len(points) < 7:
        return 0.0
    by_weekday: dict[int, list[float]] = {}
    for ts, value in points:
        by_weekday.setdefault((ts // DAY + 4) % 7, []).append(value)
    means = [sum(v) / len(v) for v in by_weekday.values() if v]
    if len(means) < 2:
        return 0.0
    overall = sum(means) / len(means)
    if overall <= 0:
        return 0.0
    return (max(means) - min(means)) / overall


def _peak_relative(values: list[float]) -> float:
    """Where the window ends relative to its own peak. 1.0 means it ends at
    the high; 0.3 means the spike already passed."""
    peak = max(values) if values else 0.0
    if peak <= 0:
        return 0.0
    return values[-1] / peak


def _magnitude_bucket(values: list[float]) -> int:
    """Log-ish bucket of absolute level. Anchored values span orders of
    magnitude, and a model given the raw number learns the anchor, not the
    term."""
    mean = sum(values) / len(values) if values else 0.0
    if mean <= 0:
        return 0
    return max(0, min(6, int(math.log10(mean * 1000) if mean > 0 else 0)))


def _correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[:n], b[:n]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=False))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= 0 or var_b <= 0:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


def build(
    term_id: int,
    window: list[tuple[int, float]],
    *,
    other_sources: dict[str, list[tuple[int, float]]] | None = None,
    days_observed: int | None = None,
) -> FeatureVector:
    """One window -> one FeatureVector.

    `window` is (unix_ts, value) for the primary series, oldest first.
    `other_sources` is the same shape per source, used only for breadth and
    correlation.
    """
    window = sorted(window)
    values = [v for _, v in window]
    others = other_sources or {}

    breadth = 1 + sum(1 for series in others.values() if series)
    correlations = [
        _correlation(values, [v for _, v in sorted(series)])
        for series in others.values()
        if len(series) >= 3
    ]

    return FeatureVector(
        term_id=term_id,
        window_end_ts=window[-1][0] if window else 0,
        slope=_ols_slope(values),
        acceleration=_acceleration(values),
        volatility=_volatility(values),
        seasonality_amp=_seasonality_amp(window),
        peak_relative=_peak_relative(values),
        days_observed=days_observed if days_observed is not None else len(values),
        source_breadth=breadth,
        source_correlation=sum(correlations) / len(correlations) if correlations else 0.0,
        magnitude_bucket=_magnitude_bucket(values),
    )


def series_for(
    conn: sqlite3.Connection, term_id: int, source: str, metric: str
) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts, value FROM signal_snapshots "
        "WHERE term_id = ? AND source = ? AND metric = ? ORDER BY ts",
        (term_id, source, metric),
    ).fetchall()
    return [(r["ts"], r["value"]) for r in rows]
