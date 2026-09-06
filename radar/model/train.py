"""Fit the durability model on a temporal split.

`HistGradientBoostingClassifier` rather than lightgbm: no extra dependency,
trains in seconds on CPU, and exposes permutation importance, which matters
because /why has to be able to say *why* a card scored high.

One model per horizon. Three small models are simpler to reason about and to
fall back from individually than one multi-output model whose heads cannot be
retired separately.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .. import log
from ..features import FEATURE_NAMES
from .dataset import HORIZONS, Sample, temporal_split

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models"


@dataclass
class TrainedHorizon:
    horizon: int
    model: object
    n_train: int
    n_test: int
    positive_rate: float
    auc: float | None
    importance: dict[str, float]


def _xy(samples: list[Sample], horizon: int) -> tuple[list[list[float]], list[int]]:
    rows, labels = [], []
    for sample in samples:
        if horizon not in sample.labels:
            continue
        rows.append(sample.features.as_row())
        labels.append(sample.labels[horizon])
    return rows, labels


def train_horizon(
    train: list[Sample], test: list[Sample], horizon: int, *, seed: int = 0
) -> TrainedHorizon | None:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import roc_auc_score

    x_train, y_train = _xy(train, horizon)
    x_test, y_test = _xy(test, horizon)
    logger = log.get(__name__, horizon=horizon)

    if len(set(y_train)) < 2:
        # Every window in the training period shares one outcome. A classifier
        # fitted here would be a constant dressed as a model.
        logger.warning(
            "skipping horizon: training labels are all one class",
            extra={"n": len(y_train), "classes": sorted(set(y_train))},
        )
        return None

    model = HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.06, max_depth=4, random_state=seed
    )
    model.fit(x_train, y_train)

    auc = None
    if x_test and len(set(y_test)) > 1:
        auc = float(roc_auc_score(y_test, model.predict_proba(x_test)[:, 1]))

    importance: dict[str, float] = {}
    if x_test and len(set(y_test)) > 1:
        result = permutation_importance(
            model, x_test, y_test, n_repeats=5, random_state=seed, scoring="roc_auc"
        )
        importance = {
            name: float(value)
            for name, value in zip(FEATURE_NAMES, result.importances_mean, strict=True)
        }

    return TrainedHorizon(
        horizon=horizon,
        model=model,
        n_train=len(y_train),
        n_test=len(y_test),
        positive_rate=sum(y_train) / len(y_train),
        auc=auc,
        importance=importance,
    )


def train_all(
    samples: list[Sample],
    *,
    holdout_fraction: float = 0.2,
    horizons: tuple[int, ...] = HORIZONS,
    out_dir: Path | None = None,
) -> dict[int, TrainedHorizon]:
    import joblib

    logger = log.get(__name__)
    train, test = temporal_split(samples, holdout_fraction=holdout_fraction)
    logger.info(
        "split", extra={"train": len(train), "test": len(test), "total": len(samples)}
    )

    directory = out_dir or MODEL_DIR
    directory.mkdir(parents=True, exist_ok=True)

    trained: dict[int, TrainedHorizon] = {}
    for horizon in horizons:
        result = train_horizon(train, test, horizon)
        if result is None:
            continue
        trained[horizon] = result
        joblib.dump(result.model, directory / f"durability_{horizon}.pkl")
        logger.info(
            "trained",
            extra={
                "horizon": horizon,
                "n_train": result.n_train,
                "n_test": result.n_test,
                "positive_rate": round(result.positive_rate, 3),
                "auc": None if result.auc is None else round(result.auc, 3),
            },
        )

    write_metadata(directory, trained)
    return trained


def write_metadata(directory: Path, trained: dict[int, TrainedHorizon]) -> None:
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "features": FEATURE_NAMES,
                "horizons": {
                    str(h): {
                        "n_train": t.n_train,
                        "n_test": t.n_test,
                        "positive_rate": t.positive_rate,
                        "auc": t.auc,
                        "importance": t.importance,
                    }
                    for h, t in trained.items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def record_backtest(block: dict, directory: Path | None = None) -> Path:
    """Merge the backtest verdict into models/metadata.json.

    Written as a separate step because the backtest can only run after
    training, but the loader has to see both. Until this block lands, the
    model is unmeasured and `DurabilityModel.load()` will refuse it.
    """
    path = (directory or MODEL_DIR) / "metadata.json"
    metadata = {}
    if path.exists():
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
    metadata["backtest"] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return path
