"""Backtest with baselines. Mandatory, per PROMPT.md M5.

    Report precision@10 for the model against random ordering and against
    naive momentum (rank by 7-day slope alone).

    If the model does not beat naive momentum, docs/MODEL.md must say so in
    plain language and the pipeline must fall back to momentum. Shipping a
    model that loses to a one-line heuristic and not saying so is the single
    worst outcome of this project.

So `verdict()` returns the honest answer and `should_use_model()` is what the
scorer asks. Neither is optional and neither is decorative: a model that loses
is expected to lose loudly.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .dataset import Sample

# A ranker that cannot separate the classes on the full test set has no
# business on a card, however well it did on ten rows. precision@10 over 25
# terms saturates and ties easily; AUC over thousands of windows does not, so
# it is the floor rather than the headline.
AUC_FLOOR = 0.55


@dataclass
class BacktestResult:
    horizon: int
    n: int
    k: int
    base_rate: float
    model_precision: float | None
    momentum_precision: float
    random_precision: float
    # Same comparison at a wider k, used only to break a tie on the headline
    # metric. Declared before looking at the numbers, not chosen after.
    tiebreak_k: int = 0
    wide_model_precision: float | None = None
    wide_momentum_precision: float | None = None
    auc: float | None = None

    @property
    def beats_momentum(self) -> bool:
        return self.model_precision is not None and self.model_precision > self.momentum_precision

    @property
    def beats_random(self) -> bool:
        return self.model_precision is not None and self.model_precision > self.random_precision

    @property
    def ties_momentum(self) -> bool:
        return (
            self.model_precision is not None
            and self.model_precision == self.momentum_precision
        )

    @property
    def clears_auc_floor(self) -> bool:
        # No AUC recorded means the horizon was never measured that way; the
        # floor cannot be waived by simply not measuring it.
        return self.auc is not None and self.auc >= AUC_FLOOR

    @property
    def wins_tiebreak(self) -> bool:
        if self.wide_model_precision is None or self.wide_momentum_precision is None:
            return False
        return self.wide_model_precision > self.wide_momentum_precision

    @property
    def use_model(self) -> bool:
        """The single question the scorer asks about this horizon.

        Three conditions, all of them arguable only in the strict direction:

        1. Beat random. A ranker that cannot do this is noise.
        2. Beat momentum at precision@k -- the bar PROMPT.md sets. A *tie* on
           this metric is not a win, but it is not evidence either: with 25
           terms both rankers hit 1.00 and the metric has simply run out of
           resolution. Only in that case is the same comparison at ten times k
           consulted, and only as a tiebreak.
        3. Clear the AUC floor on the full test set. This is what stops a
           lucky top-ten from promoting a head that is otherwise a coin flip.
        """
        if not self.beats_random or not self.clears_auc_floor:
            return False
        if self.beats_momentum:
            return True
        return self.ties_momentum and self.wins_tiebreak

    def as_dict(self) -> dict:
        return {
            "horizon": self.horizon,
            "n": self.n,
            "k": self.k,
            "base_rate": self.base_rate,
            "model_precision": self.model_precision,
            "momentum_precision": self.momentum_precision,
            "random_precision": self.random_precision,
            "tiebreak_k": self.tiebreak_k,
            "wide_model_precision": self.wide_model_precision,
            "wide_momentum_precision": self.wide_momentum_precision,
            "auc": self.auc,
            "use_model": self.use_model,
        }


def precision_at_k(ranked_labels: list[int], k: int) -> float:
    top = ranked_labels[:k]
    return sum(top) / len(top) if top else 0.0


def _ranked(scored: list[tuple[float, int]]) -> list[int]:
    """Highest score first; labels in that order."""
    return [label for _, label in sorted(scored, key=lambda pair: pair[0], reverse=True)]


def run(
    test: list[Sample],
    model,
    horizon: int,
    *,
    k: int = 10,
    seed: int = 0,
    random_trials: int = 200,
    tiebreak_k: int | None = None,
    auc: float | None = None,
) -> BacktestResult:
    usable = [s for s in test if horizon in s.labels]
    labels = [s.labels[horizon] for s in usable]
    base_rate = sum(labels) / len(labels) if labels else 0.0

    tiebreak_k = tiebreak_k if tiebreak_k is not None else k * 10

    model_precision = None
    wide_model_precision = None
    if model is not None and usable:
        probabilities = model.predict_proba([s.features.as_row() for s in usable])[:, 1]
        ranked = _ranked(list(zip(probabilities, labels, strict=True)))
        model_precision = precision_at_k(ranked, k)
        wide_model_precision = precision_at_k(ranked, tiebreak_k)

    # Naive momentum: rank by 7-day slope alone. One line, no training, and the
    # bar the model has to clear to be worth existing.
    ranked_by_slope = _ranked(
        [(s.features.slope, label) for s, label in zip(usable, labels, strict=True)]
    )
    momentum_precision = precision_at_k(ranked_by_slope, k)
    wide_momentum_precision = precision_at_k(ranked_by_slope, tiebreak_k)

    # Random ordering, averaged over trials so a lucky shuffle proves nothing.
    rng = random.Random(seed)
    random_scores = []
    for _ in range(random_trials):
        shuffled = labels[:]
        rng.shuffle(shuffled)
        random_scores.append(precision_at_k(shuffled, k))
    random_precision = sum(random_scores) / len(random_scores) if random_scores else 0.0

    return BacktestResult(
        horizon=horizon,
        n=len(usable),
        k=k,
        base_rate=base_rate,
        model_precision=model_precision,
        momentum_precision=momentum_precision,
        random_precision=random_precision,
        tiebreak_k=tiebreak_k,
        wide_model_precision=wide_model_precision,
        wide_momentum_precision=wide_momentum_precision,
        auc=auc,
    )


def verdict(results: list[BacktestResult]) -> str:
    """Plain language, for docs/MODEL.md and for /why."""
    if not results:
        return "No backtest was possible: there were no labelled windows to test on."

    lines = []
    for r in results:
        model = "n/a" if r.model_precision is None else f"{r.model_precision:.2f}"
        lines.append(
            f"+{r.horizon}d  n={r.n:<6} base={r.base_rate:.2f}  "
            f"model={model}  momentum={r.momentum_precision:.2f}  "
            f"random={r.random_precision:.2f}"
        )

    winners = [r for r in results if r.beats_momentum]
    if not winners:
        lines.append("")
        lines.append(
            "The model does not beat naive momentum at any horizon. Momentum is "
            "the better ranker, so scoring falls back to it and the cards say "
            "'momentum_fallback'. This is the honest outcome, not a bug to hide."
        )
    elif len(winners) < len(results):
        beaten = ", ".join(f"+{r.horizon}d" for r in results if not r.beats_momentum)
        lines.append("")
        lines.append(
            f"The model beats momentum at some horizons but not at {beaten}. "
            "Use the model only where it wins; fall back elsewhere."
        )
    else:
        lines.append("")
        lines.append("The model beats naive momentum at every horizon tested.")
    return "\n".join(lines)


def should_use_model(results: list[BacktestResult], horizon: int) -> bool:
    """What the scorer asks before trusting a model file.

    Losing to momentum means momentum gets used. Silent substitution is
    forbidden either way -- whichever wins, `Score.scorer` records which.
    """
    for r in results:
        if r.horizon == horizon:
            return r.use_model
    return False


def to_metadata(results: list[BacktestResult]) -> dict:
    """The block train_model.py writes into models/metadata.json.

    Persisting this is what turns the honesty clause from a promise in a
    document into something the loader enforces: `DurabilityModel.load()`
    reads `use_model` and refuses the heads that lost.
    """
    return {
        "k": results[0].k if results else None,
        "verdict": verdict(results),
        "horizons": {str(r.horizon): r.as_dict() for r in results},
    }


def allowed_from_metadata(metadata: dict) -> list[int] | None:
    """Horizons a stored backtest cleared, or None if there is no backtest.

    None means "unknown", and the loader treats unknown as untrusted. A model
    file that has never been measured against momentum has not earned a card.
    """
    block = (metadata or {}).get("backtest")
    if not block:
        return None
    horizons = (block or {}).get("horizons") or {}
    return sorted(int(h) for h, r in horizons.items() if (r or {}).get("use_model"))
