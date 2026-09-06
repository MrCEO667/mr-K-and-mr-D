"""Load the trained model and score live windows.

Two rules from the contracts, both about honesty rather than accuracy:

* `Score.scorer` is mandatory. Whatever produced the number -- "model:v1" or
  "momentum_fallback" -- says so, and /why shows it.
* **Silent substitution is forbidden.** A missing model file, or a model that
  lost its backtest, falls back to momentum *loudly*.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .. import log
from ..features import FeatureVector
from . import backtest
from .dataset import HORIZONS
from .train import MODEL_DIR

MOMENTUM_SCORER = "momentum_fallback"


@dataclass
class Durability:
    scores: dict[int, float]  # horizon -> probability
    scorer: str
    # Horizons whose number actually came from the model. The rest were filled
    # by momentum, and a card must not be able to claim otherwise.
    model_horizons: tuple[int, ...] = ()

    def scorer_for(self, horizon: int) -> str:
        """What produced *this* horizon's number.

        `scorer` describes the bundle; a card shows one horizon. Reading the
        bundle-level string off a momentum-filled horizon would label a
        fallback as model output, which is the silent substitution the
        contract forbids.
        """
        return f"model:{horizon}" if horizon in self.model_horizons else MOMENTUM_SCORER


class DurabilityModel:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or MODEL_DIR
        self._models: dict[int, object] = {}
        self.metadata: dict = {}
        self.loaded_horizons: list[int] = []

    def load(self, *, allowed_horizons: list[int] | None = None) -> DurabilityModel:
        """Load only the heads that earned their place.

        `allowed_horizons` defaults to whatever the stored backtest cleared, so
        the honesty clause is enforced by the loader rather than by remembering
        to pass the right argument. A model directory with no recorded
        backtest loads nothing at all: unmeasured is not the same as good, and
        the difference has to cost something or it will be skipped.
        """
        import joblib

        logger = log.get(__name__)
        metadata_path = self.directory / "metadata.json"
        if metadata_path.exists():
            try:
                self.metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.metadata = {}

        if allowed_horizons is None:
            allowed_horizons = backtest.allowed_from_metadata(self.metadata)
            if allowed_horizons is None:
                logger.warning(
                    "model has never been backtested; refusing it and falling "
                    "back to momentum. Run scripts/train_model.py.",
                    extra={"looked_in": str(self.directory)},
                )
                allowed_horizons = []
            else:
                lost = [h for h in HORIZONS if h not in allowed_horizons]
                if lost:
                    logger.info(
                        "horizons lost their backtest and will use momentum",
                        extra={"lost": lost, "kept": allowed_horizons},
                    )

        for horizon in HORIZONS:
            if allowed_horizons is not None and horizon not in allowed_horizons:
                # Lost its backtest. Not loading it is what keeps the fallback
                # from being silent.
                continue
            path = self.directory / f"durability_{horizon}.pkl"
            if not path.exists():
                continue
            try:
                self._models[horizon] = joblib.load(path)
            except Exception as exc:  # noqa: BLE001 -- a bad pickle is a fallback, not a crash
                logger.error("model failed to load", extra={"horizon": horizon, "error": str(exc)})

        self.loaded_horizons = sorted(self._models)
        if not self._models:
            logger.warning(
                "no usable durability model; scoring falls back to momentum",
                extra={"looked_in": str(self.directory)},
            )
        return self

    @property
    def available(self) -> bool:
        return bool(self._models)

    def score(self, vector: FeatureVector) -> Durability:
        """Probability per horizon, or the momentum fallback.

        The fallback is a squashed slope: it is a ranking signal, not a
        calibrated probability, and `scorer` says so on every card that uses it.
        """
        if not self._models:
            return Durability(
                scores={h: _momentum_score(vector) for h in HORIZONS},
                scorer=MOMENTUM_SCORER,
                model_horizons=(),
            )

        row = [vector.as_row()]
        scores = {
            horizon: float(model.predict_proba(row)[0][1])
            for horizon, model in self._models.items()
        }
        model_horizons = tuple(sorted(scores))
        for horizon in HORIZONS:
            scores.setdefault(horizon, _momentum_score(vector))
        return Durability(
            scores=scores,
            scorer=f"model:{sorted(self._models)}",
            model_horizons=model_horizons,
        )


def _momentum_score(vector: FeatureVector) -> float:
    """Naive momentum, squashed to 0-1 for comparability.

    Deliberately the same one-line heuristic the backtest measures the model
    against, so a fallback score and the baseline are the same number.
    """
    import math

    return 1 / (1 + math.exp(-vector.slope * 10))
