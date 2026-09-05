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
from . import db, log

STAGES = ("discover", "collect", "saturation", "score", "compose", "deliver")


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
        logger.error("enabled source has no credentials", extra={"source": source, "missing": keys})
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


def execute(conn: sqlite3.Connection, cfg: config_module.Config, kind: str, *, dry_run: bool) -> str:
    """Open a run and walk the stages. Stages are no-ops until their milestone
    lands -- they are listed so the wiring is visible from day one."""
    with db.run(conn, kind, dry_run=dry_run) as run_id:
        logger = log.get(__name__, run_id=run_id)
        for stage in STAGES:
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
