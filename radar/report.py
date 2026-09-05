"""CLI inspection. `python -m radar.report --term "ai voice clone"`

M1 is done when two runs an hour apart produce two distinct snapshot rows per
term and this prints the series, so this is the milestone's proof, not a
convenience.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from . import config as config_module
from . import db


def format_series(rows: Sequence[sqlite3.Row], term: str) -> str:
    if not rows:
        return f"No snapshots for {term!r} yet."

    lines = [f"{term}  --  {len(rows)} snapshots", ""]
    values = [r["value"] for r in rows]
    width = 40
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    for row in rows:
        stamp = datetime.fromtimestamp(row["ts"], tz=UTC).strftime("%Y-%m-%d %H:%M")
        bar = "#" * max(1, int((row["value"] - lo) / span * width))
        lines.append(f"{stamp}  {row['value']:8.3f}  {bar}")

    runs = {r["run_id"] for r in rows}
    lines += [
        "",
        f"range {lo:.3f} to {hi:.3f} (anchored, unitless)",
        f"collected across {len(runs)} run(s)",
    ]
    return "\n".join(lines)


def summarise(conn: sqlite3.Connection) -> str:
    counts = conn.execute(
        "SELECT t.term, COUNT(*) AS n, MAX(s.ts) AS latest "
        "FROM signal_snapshots s JOIN terms t ON t.id = s.term_id "
        "GROUP BY t.term ORDER BY n DESC, t.term LIMIT 20"
    ).fetchall()
    if not counts:
        return "No snapshots stored yet. Run: python -m radar --once"
    lines = ["term                                 snapshots  latest", ""]
    for row in counts:
        latest = datetime.fromtimestamp(row["latest"], tz=UTC).strftime("%Y-%m-%d")
        lines.append(f"{row['term']:<36} {row['n']:>9}  {latest}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m radar.report")
    parser.add_argument("--term", help="print the stored time series for one term")
    parser.add_argument("--source", default=None, help="restrict to one source")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"config: {exc}")
        return 2

    conn = db.connect(cfg.db_path)
    try:
        if args.term:
            print(format_series(db.series(conn, args.term, source=args.source), args.term))
        else:
            print(summarise(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
