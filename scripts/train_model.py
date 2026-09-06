"""Build the dataset, train, backtest against baselines, print the verdict.

    python scripts/train_model.py
    python scripts/train_model.py --sweep      # try several label thresholds

The verdict goes into docs/MODEL.md by hand -- including, and especially, when
the model loses to momentum.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar import config as config_module  # noqa: E402
from radar import db, log  # noqa: E402
from radar.model import backtest as backtest_mod  # noqa: E402
from radar.model import dataset as dataset_mod  # noqa: E402
from radar.model import train as train_mod  # noqa: E402


def build(conn, threshold: float, stride: int):
    return dataset_mod.build_samples(conn, threshold=threshold, stride=stride)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.6)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--holdout", type=float, default=0.2)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--sweep", action="store_true", help="try several thresholds")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    log.setup("INFO", json_output=False)
    cfg = config_module.load(args.config)
    conn = db.connect(cfg.db_path)

    thresholds = [0.4, 0.5, 0.6, 0.7, 0.8] if args.sweep else [args.threshold]
    for threshold in thresholds:
        samples = build(conn, threshold, args.stride)
        print(f"\n=== label threshold {threshold} ===")
        print(f"samples: {len(samples)}")
        if not samples:
            print("no labelled windows; backfill more history first")
            continue

        counts = {}
        for sample in samples:
            for horizon, label in sample.labels.items():
                bucket = counts.setdefault(horizon, [0, 0])
                bucket[label] += 1
        for horizon in sorted(counts):
            negative, positive = counts[horizon]
            total = negative + positive
            print(f"  +{horizon}d  n={total:<6} positive={positive / total:.2%}")

        # A sweep trains once per threshold. Writing those into models/ would
        # leave whichever threshold happened to run last sitting on disk as
        # "the" model, so a sweep is explicitly a measurement, not a build.
        out_dir = None if not args.sweep else Path(tempfile.mkdtemp(prefix="sweep-"))
        trained = train_mod.train_all(
            samples, holdout_fraction=args.holdout, out_dir=out_dir
        )
        _, test = dataset_mod.temporal_split(samples, holdout_fraction=args.holdout)

        results = []
        for horizon in dataset_mod.HORIZONS:
            head = trained.get(horizon)
            results.append(
                backtest_mod.run(
                    test,
                    head.model if head else None,
                    horizon,
                    k=args.k,
                    tiebreak_k=args.k * 10,
                    auc=head.auc if head else None,
                )
            )

        print("")
        print(f"BACKTEST  precision@{args.k}")
        print(backtest_mod.verdict(results))

        # precision@10 over a handful of terms is a very small sample. A second
        # k is printed alongside it because a result that flips between the two
        # is noise, and that is worth seeing before anyone believes it.
        print("")
        print(f"stability check  precision@{args.k * 10} (tiebreak) and AUC on the full test set")
        for r in results:
            wide = "n/a" if r.wide_model_precision is None else f"{r.wide_model_precision:.2f}"
            auc = "n/a" if r.auc is None else f"{r.auc:.2f}"
            momentum = (
                "n/a" if r.wide_momentum_precision is None else f"{r.wide_momentum_precision:.2f}"
            )
            print(
                f"+{r.horizon}d  n={r.n:<6} model={wide}  momentum={momentum}  "
                f"auc={auc}  use_model={r.use_model}"
            )

        if out_dir is None:
            block = backtest_mod.to_metadata(results)
            path = train_mod.record_backtest(block)
            kept = backtest_mod.allowed_from_metadata({"backtest": block})
            print("")
            print(f"recorded verdict -> {path}")
            print(f"horizons the loader will trust: {kept or 'none, momentum fallback'}")
        else:
            shutil.rmtree(out_dir, ignore_errors=True)

        for horizon, result in trained.items():
            top = sorted(result.importance.items(), key=lambda kv: kv[1], reverse=True)[:4]
            if top:
                shown = ", ".join(f"{name} {value:+.3f}" for name, value in top)
                print(f"  +{horizon}d importance: {shown}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
