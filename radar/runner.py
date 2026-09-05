"""Orchestration and the single entry point.

M0 wires the skeleton only: config in, database up, a run row opened and
closed honestly, structured logs carrying the run_id. The pipeline stages
attach here as they land, in the order set out in docs/WORKFLOW.md.

    python -m radar --once --kind sweep
    python -m radar --once --dry-run          # writes nothing, proves the wiring
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from . import config as config_module
from . import db, discover, log
from .cache import Cache
from .collectors.base import RateLimiter, SourceHealth, SourceUnavailable
from .collectors.github import GitHubCollector
from .collectors.hackernews import HackerNewsCollector
from .collectors.http import JsonHttp
from .collectors.trends import TrendsCollector

# Stages still to land keep their place in the list so the shape of a run is
# visible from the logs before the code exists.
PENDING_STAGES = ("saturation", "score", "compose", "deliver")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m radar",
        description="NicheRadar — local opportunity scanner.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single pass and exit (the only supported mode until the scheduler lands)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything, then roll the transaction back so nothing is written",
    )
    parser.add_argument(
        "--kind",
        default="sweep",
        choices=db.VALID_KINDS,
        help="which run this is; recorded on the runs row (default: sweep)",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--plain-logs",
        action="store_true",
        help="human-readable logs instead of JSON lines",
    )
    return parser


def preflight(cfg: config_module.Config, logger) -> bool:
    """Report what this run cannot do before it pretends to do it.

    Returns False when an enabled source has no credentials. Retrend shipped a
    complete YouTube collector and collected zero rows because the key was
    never provisioned; that failure was silent, which is the part worth fixing.
    """
    healthy = True
    missing = cfg.missing_secrets()
    for source, keys in missing.items():
        logger.error(
            "enabled source has no credentials",
            extra={"source": source, "missing": keys},
        )
        healthy = False
    logger.info(
        "preflight",
        extra={
            "sources_enabled": cfg.enabled_sources(),
            "operators": [op.name for op in cfg.operators],
            "db": str(cfg.db_path),
        },
    )
    return healthy


def build_collectors(cfg: config_module.Config, *, cache: Cache | None = None) -> list:
    """Only enabled sources, and only ones that are built.

    A source enabled in config but not yet implemented is silently absent
    rather than a crash -- config.example.yaml enables everything by design.
    """
    enabled = set(cfg.enabled_sources())
    shared_cache = cache if cache is not None else Cache()
    collectors: list = []

    if "google_trends" in enabled:
        rate = cfg.get("sources.google_trends.rate_limit_s", 4)
        collectors.append(TrendsCollector(rate_limiter=RateLimiter(min_interval_s=float(rate))))

    if "hackernews" in enabled:
        collectors.append(
            HackerNewsCollector(
                http=JsonHttp(
                    HackerNewsCollector.source,
                    rate_limiter=RateLimiter(min_interval_s=1.0),
                    cache=shared_cache,
                )
            )
        )

    if "github" in enabled:
        from .collectors.github import MIN_INTERVAL_S

        collectors.append(
            GitHubCollector(
                http=JsonHttp(
                    GitHubCollector.source,
                    rate_limiter=RateLimiter(min_interval_s=MIN_INTERVAL_S),
                    cache=shared_cache,
                )
            )
        )

    return collectors


def run_collectors(
    conn: sqlite3.Connection,
    collectors: list,
    terms: list,
    run_id: str,
) -> int:
    """Collect, persist, record health. One dead source never fails a run."""
    logger = log.get(__name__, run_id=run_id)
    total = 0

    for collector in collectors:
        try:
            result = collector.collect(terms, run_id)
        except SourceUnavailable as exc:
            # Degrade, never crash. The run continues with reduced breadth and
            # says so on the runs row and in source_health.
            db.record_source_health(
                conn, run_id, collector.source, status="down", message=str(exc)
            )
            db.mark_degraded(conn, run_id, f"{collector.source} unavailable: {exc}")
            logger.error("source down", extra={"source": collector.source, "error": str(exc)})
            continue

        written = db.write_readings(conn, run_id, result.readings)
        if result.evidence:
            db.write_evidence(conn, run_id, result.evidence)
        total += written

        health: SourceHealth = collector.health()
        db.record_source_health(
            conn,
            run_id,
            health.source,
            status=health.status,
            latency_ms=health.latency_ms,
            error_count=health.error_count,
            message=health.message,
        )
        if result.partial:
            db.mark_degraded(
                conn, run_id, f"{collector.source} partial: {len(result.errors)} term(s) failed"
            )
        logger.info(
            "source done",
            extra={
                "source": collector.source,
                "readings": len(result.readings),
                "new_rows": written,
                "partial": result.partial,
                "status": health.status,
            },
        )
    return total


def execute(
    conn: sqlite3.Connection,
    cfg: config_module.Config,
    kind: str,
    *,
    dry_run: bool,
) -> str:
    """Open a run and walk the stages that exist."""
    with db.run(conn, kind, dry_run=dry_run) as run_id:
        logger = log.get(__name__, run_id=run_id)

        terms = discover.seed_terms(conn)
        if kind == "watchlist":
            terms = [t for t in terms if t.starred]
        logger.info("terms selected", extra={"count": len(terms), "kind": kind})

        written = run_collectors(conn, build_collectors(cfg), terms, run_id)
        logger.info("snapshots written", extra={"new_rows": written})

        for stage in PENDING_STAGES:
            logger.info("stage skipped, not implemented yet", extra={"stage": stage})
        return run_id


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log.setup(args.log_level, json_output=not args.plain_logs)
    logger = log.get(__name__)

    if not args.once:
        logger.error("only --once is supported; the scheduler is not built yet")
        return 2

    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        logger.error("config rejected", extra={"error": str(exc)})
        return 2

    conn = db.connect(cfg.db_path)
    try:
        if not preflight(cfg, logger) and not args.dry_run:
            logger.error("refusing to run: a source is enabled but cannot authenticate")
            return 1
        run_id = execute(conn, cfg, args.kind, dry_run=args.dry_run)
    finally:
        conn.close()

    logger.info("done", extra={"run_id": run_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
