"""Composite scoring: the ranking that decides what is worth composing.

    composite = w_d*durability + w_s*(1 - saturation) + w_r*relevance

from `scoring.weights` in config. The feasibility gate is deliberately *not* a
term here -- docs/SPEC.md makes it a multiplier that zeroes the composite, and
it applies to a composed opportunity rather than to a term, because setup cost
and margin do not exist until there is a playbook. So this ranks demand; M6
zeroes whatever it rejects afterwards.

`scorer` is mandatory on every row and says what actually produced the
durability number -- "model:30" or "momentum_fallback" -- because the model is
refused at some horizons and silent substitution is forbidden. The relevance
term is a neutral constant until M9 has labels, and the scorer string says that
too rather than letting 0.5 pass for an opinion.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db, features, log, saturation
from .model.dataset import PRIMARY_METRIC, PRIMARY_SOURCE
from .model.predict import MOMENTUM_SCORER, DurabilityModel

# Until M9 has 100 decisions there is no personal model, so every term gets the
# same relevance and it cancels out of the ranking. Stated on the card rather
# than hidden, per PROMPT.md M9.
NEUTRAL_RELEVANCE = 0.5
RELEVANCE_NEUTRAL_SCORER = "relevance:neutral"

# Label -> how saturated, 0 (empty field) to 1 (crowded). Derived at score time
# so the thresholds in config stay tunable, per M4.
SATURATION_NORM = {"LOW": 0.0, "MED": 0.5, "HIGH": 1.0}

GROWTH_WINDOW_DAYS = 7
DAY = features.DAY


@dataclass
class Score:
    term_id: int
    durability: dict[int, float]
    saturation_label: str
    saturation_raw: int
    demand_growth: float
    supply_growth: float
    relevance: float
    composite: float
    scorer: str
    horizon: int = 60
    notes: list[str] = field(default_factory=list)


def growth(series: list[tuple[int, float]], *, window_days: int = GROWTH_WINDOW_DAYS) -> float:
    """Fractional change of the last `window_days` against the `window_days`
    before them. 0.0 when there is not enough history to compare.

    Returned as a fraction (0.34 = +34%), never a percent, so nothing downstream
    has to guess which one it is holding.
    """
    if not series:
        return 0.0
    by_day: dict[int, float] = {}
    for ts, value in sorted(series):
        by_day[ts // DAY] = value
    days = sorted(by_day)
    if len(days) < 2:
        return 0.0

    end = days[-1]
    recent = [by_day[d] for d in days if end - window_days < d <= end]
    prior = [by_day[d] for d in days if end - 2 * window_days < d <= end - window_days]
    if not recent or not prior:
        return 0.0

    before = sum(prior) / len(prior)
    after = sum(recent) / len(recent)
    if before <= 0:
        # Rising from nothing is real, but a division by zero is not a number.
        return 1.0 if after > 0 else 0.0
    return (after - before) / before


def _latest_window(
    conn: sqlite3.Connection, term_id: int
) -> tuple[list[tuple[int, float]], int] | None:
    """The most recent full 14-day primary window, and days observed."""
    series = features.series_for(conn, term_id, PRIMARY_SOURCE, PRIMARY_METRIC)
    if not series:
        return None
    by_day: dict[int, float] = {}
    for ts, value in sorted(series):
        by_day.setdefault(ts // DAY, value)
    days = sorted(by_day)
    end = days[-1]
    window = [
        (d * DAY, by_day[d])
        for d in range(end - features.WINDOW_DAYS + 1, end + 1)
        if d in by_day
    ]
    if len(window) < features.WINDOW_DAYS:
        return None
    return window, end - days[0] + 1


def score_term(
    conn: sqlite3.Connection,
    cfg,
    term_id: int,
    model: DurabilityModel,
) -> Score | None:
    """One term -> one Score, or None when there is not enough history."""
    horizon = int(cfg.get("scoring.durability_horizon", 60))
    weights = cfg.get("scoring.weights", {}) or {}
    thresholds = cfg.get("scoring.saturation_thresholds", {}) or {}

    latest = _latest_window(conn, term_id)
    if latest is None:
        return None
    window, days_observed = latest

    others = {
        source: features.series_for(conn, term_id, source, metric)
        for source, metric in (
            ("youtube", "view_sum"),
            ("hackernews", "post_count"),
            ("github", "stars"),
        )
    }
    vector = features.build(
        term_id,
        window,
        days_observed=days_observed,
        other_sources={k: v for k, v in others.items() if v},
    )

    durability = model.score(vector)
    counts = saturation.latest_counts(conn, term_id)
    raw = sum(counts.values())
    label = saturation.label(
        raw,
        low_max=int(thresholds.get("low_max", 200)),
        med_max=int(thresholds.get("med_max", 2000)),
    )

    demand = growth(features.series_for(conn, term_id, PRIMARY_SOURCE, PRIMARY_METRIC))
    supply = _supply_growth(conn, term_id)

    d = durability.scores.get(horizon, 0.0)
    composite = (
        float(weights.get("durability", 0.45)) * d
        + float(weights.get("saturation", 0.35)) * (1.0 - SATURATION_NORM.get(label, 0.5))
        + float(weights.get("relevance", 0.20)) * NEUTRAL_RELEVANCE
    )

    scorer = f"{durability.scorer_for(horizon)}+{RELEVANCE_NEUTRAL_SCORER}"
    notes = []
    if durability.scorer_for(horizon) == MOMENTUM_SCORER:
        notes.append(
            f"Durability at +{horizon}d is naive momentum, not the model: that "
            "head did not beat momentum in the backtest."
        )
    if not counts:
        notes.append("No supply counts yet, so saturation is unmeasured rather than low.")

    return Score(
        term_id=term_id,
        durability=durability.scores,
        saturation_label=label,
        saturation_raw=raw,
        demand_growth=demand,
        supply_growth=supply,
        relevance=NEUTRAL_RELEVANCE,
        composite=composite,
        scorer=scorer,
        horizon=horizon,
        notes=notes,
    )


def _supply_growth(conn: sqlite3.Connection, term_id: int) -> float:
    """Growth of total supply counts over time.

    The signal that matters is supply growth *against* demand growth: demand
    rising while supply is flat is the window, both rising is a race already
    lost (PROMPT.md M4). One count is not a trend, so it reports 0.0.
    """
    rows = conn.execute(
        "SELECT ts, SUM(count) AS total FROM saturation_snapshots "
        "WHERE term_id = ? GROUP BY ts ORDER BY ts",
        (term_id,),
    ).fetchall()
    return growth([(r["ts"], float(r["total"])) for r in rows])


def score_terms(
    conn: sqlite3.Connection, cfg, terms, model: DurabilityModel | None = None
) -> list[Score]:
    logger = log.get(__name__)
    model = model if model is not None else DurabilityModel().load()
    scores = []
    for term in terms:
        score = score_term(conn, cfg, term.id, model)
        if score is None:
            logger.info("term skipped, not enough history", extra={"term_id": term.id})
            continue
        scores.append(score)
    scores.sort(key=lambda s: s.composite, reverse=True)
    logger.info("terms scored", extra={"count": len(scores)})
    return scores


def write_scores(conn: sqlite3.Connection, run_id: str, scores: list[Score]) -> int:
    """Insert without committing: `db.run` owns the transaction.

    Committing here would end it early, so the run's own COMMIT/ROLLBACK fails
    -- and --dry-run, which promises to write nothing, would have written.
    """
    now = db.now()
    for score in scores:
        conn.execute(
            "INSERT INTO scores (term_id, run_id, durability_30, durability_60, "
            "durability_90, saturation_label, saturation_raw, demand_growth, "
            "supply_growth, relevance, composite, scorer, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                score.term_id,
                run_id,
                score.durability.get(30),
                score.durability.get(60),
                score.durability.get(90),
                score.saturation_label,
                score.saturation_raw,
                score.demand_growth,
                score.supply_growth,
                score.relevance,
                score.composite,
                score.scorer,
                now,
            ),
        )
    return len(scores)
