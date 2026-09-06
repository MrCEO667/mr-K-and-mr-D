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

from . import compose as compose_mod
from . import config as config_module
from . import db, discover, feasibility, log, saturation
from . import score as score_mod
from .cache import Cache
from .collectors.base import RateLimiter, SourceHealth, SourceUnavailable
from .collectors.github import GitHubCollector
from .collectors.hackernews import HackerNewsCollector
from .collectors.http import JsonHttp
from .collectors.producthunt import ProductHuntCollector
from .collectors.reddit import RedditCollector
from .collectors.trends import TrendsCollector
from .collectors.youtube import YouTubeCollector

# Stages still to land keep their place in the list so the shape of a run is
# visible from the logs before the code exists.
PENDING_STAGES = ("deliver",)

# Composing is the slow part of a run: a local 8B model takes 20-40 seconds per
# term on this hardware, so a full sweep would sit in the LLM for a quarter of
# an hour. Only the best few terms are worth that, and the rest are scored and
# left for the next run.
DEFAULT_COMPOSE_LIMIT = 5


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
    parser.add_argument(
        "--harvest",
        action="store_true",
        help="mine new terms from Hacker News titles before collecting (opt-in: "
        "harvest quality is unproven and each new term costs quota every sweep)",
    )
    parser.add_argument(
        "--compose",
        action="store_true",
        help="compose playbooks for the top-scoring terms with the local LLM "
        "(off by default: it needs Ollama running and takes ~30s per term)",
    )
    parser.add_argument(
        "--compose-limit",
        type=int,
        default=DEFAULT_COMPOSE_LIMIT,
        help=f"how many top terms to compose (default: {DEFAULT_COMPOSE_LIMIT})",
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

    if "youtube" in enabled:
        collectors.append(
            YouTubeCollector(
                http=JsonHttp(
                    YouTubeCollector.source,
                    rate_limiter=RateLimiter(min_interval_s=0.5),
                    cache=shared_cache,
                )
            )
        )

    if "reddit" in enabled:
        collectors.append(RedditCollector())

    if "product_hunt" in enabled:
        collectors.append(ProductHuntCollector())

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
    harvest: bool = False,
    compose_limit: int = 0,
) -> str:
    """Open a run and walk the stages that exist."""
    with db.run(conn, kind, dry_run=dry_run) as run_id:
        logger = log.get(__name__, run_id=run_id)

        if harvest:
            added = discover.harvest_hackernews(
                conn, max_new_terms=int(cfg.get("discovery.max_new_terms_per_run", 50))
            )
            logger.info("harvested terms", extra={"added": len(added)})

        terms = discover.seed_terms(conn)
        if kind == "watchlist":
            terms = [t for t in terms if t.starred]
        logger.info("terms selected", extra={"count": len(terms), "kind": kind})

        written = run_collectors(conn, build_collectors(cfg), terms, run_id)
        logger.info("snapshots written", extra={"new_rows": written})

        counters = saturation.build_counters(set(cfg.enabled_sources()))
        supply = saturation.collect_saturation(conn, counters, terms, run_id)
        logger.info("supply counted", extra={"rows": supply})

        scores = score_mod.score_terms(conn, cfg, terms)
        score_mod.write_scores(conn, run_id, scores)
        logger.info(
            "terms scored",
            extra={
                "count": len(scores),
                "top": [
                    {"term_id": s.term_id, "composite": round(s.composite, 3)}
                    for s in scores[:5]
                ],
            },
        )

        if compose_limit > 0 and scores:
            composed = run_composer(conn, cfg, scores, terms, run_id, limit=compose_limit)
            logger.info("opportunities composed", extra=composed)
        else:
            logger.info("stage skipped", extra={"stage": "compose", "why": "not requested"})

        for stage in PENDING_STAGES:
            logger.info("stage skipped, not implemented yet", extra={"stage": stage})
        return run_id


def run_composer(
    conn: sqlite3.Connection,
    cfg: config_module.Config,
    scores,
    terms,
    run_id: str,
    *,
    limit: int,
) -> dict:
    """Compose the top `limit` scored terms and persist every result.

    One dead generation must not end a run, exactly as one dead source must
    not. A term that cannot be composed is written with composed=0 and the
    next term is attempted.
    """
    logger = log.get(__name__, run_id=run_id)
    names = {t.id: t.term for t in terms}
    client = compose_mod.Ollama(
        base_url=cfg.get("llm.base_url", "http://localhost:11434"),
        model=cfg.get("llm.model", "qwen3:8b"),
        timeout_s=float(cfg.get("llm.timeout_s", 120)),
    )
    caps = feasibility.Capabilities.load()

    written = feasible = failed = 0
    for score in scores[:limit]:
        result = compose_mod.compose_one(
            conn, cfg, score, names.get(score.term_id, ""), client, caps=caps
        )
        compose_mod.write_opportunity(conn, run_id, result)
        written += 1
        if not result.composed:
            failed += 1
        elif result.verdict and result.verdict.passed:
            feasible += 1
    if failed:
        logger.warning("some terms could not be composed", extra={"failed": failed})
    return {"written": written, "feasible": feasible, "not_composed": failed}


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
        run_id = execute(
            conn,
            cfg,
            args.kind,
            dry_run=args.dry_run,
            harvest=args.harvest,
            compose_limit=args.compose_limit if args.compose else 0,
        )
    finally:
        conn.close()

    logger.info("done", extra={"run_id": run_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
