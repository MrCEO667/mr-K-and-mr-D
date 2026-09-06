"""One-off: pull two years of daily Trends history for every active term.

M5 needs the past of each window to be already known. A sweep only ever sees
the last 90 days, so this walks overlapping date-range chunks instead.

    python scripts/backfill_history.py --days 720

Slow by design: one request per (chunk x batch of four terms), spaced.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar import config as config_module  # noqa: E402
from radar import db, discover, log  # noqa: E402
from radar.collectors.base import RateLimiter  # noqa: E402
from radar.collectors.trends import TrendsCollector  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=720)
    parser.add_argument("--rate", type=float, default=8.0)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    log.setup("INFO", json_output=False)
    logger = log.get(__name__)
    cfg = config_module.load(args.config)
    conn = db.connect(cfg.db_path)

    terms = discover.active_terms(conn)
    collector = TrendsCollector(
        rate_limiter=RateLimiter(min_interval_s=args.rate, max_retries=4, base_backoff_s=10)
    )

    with db.run(conn, "train") as run_id:
        result = collector.history(terms, run_id, days=args.days)
        written = db.write_readings(conn, run_id, result.readings)
        if result.partial:
            db.mark_degraded(conn, run_id, f"history partial: {len(result.errors)} chunk(s)")
        logger.info(
            "backfill done",
            extra={
                "terms": len(terms),
                "readings": len(result.readings),
                "new_rows": written,
                "errors": len(result.errors),
            },
        )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
