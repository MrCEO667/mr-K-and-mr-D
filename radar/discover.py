"""Term discovery. M1 implements the seed half; harvest arrives with M3.

Broad sweeps surface thousands of rising things two people cannot monetize --
the predecessor lost 250 of 395 candidates to relevance, not to virality. Seeds
keep discovery anchored to categories worth selling into.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import yaml

from . import log
from .collectors.base import Term
from .db import now

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEEDS = ROOT / "config" / "seeds.yaml"


def normalize(term: str) -> str:
    return " ".join(term.lower().split())


def load_seeds(path: Path | None = None) -> tuple[dict[str, list[str]], list[re.Pattern]]:
    data = yaml.safe_load((path or DEFAULT_SEEDS).read_text(encoding="utf-8")) or {}
    categories = data.get("categories") or {}
    patterns = [
        re.compile(p, re.IGNORECASE) for p in (data.get("exclude_patterns") or [])
    ]
    return categories, patterns


def is_excluded(term: str, patterns: list[re.Pattern]) -> bool:
    """Financial instruments and the rest of the stoplist are dropped here, at
    discovery, which is the only place PROMPT.md allows them to be dropped."""
    return any(p.search(term) for p in patterns)


def seed_terms(conn: sqlite3.Connection, path: Path | None = None) -> list[Term]:
    """Insert seed terms, then return every active term to collect on.

    Idempotent: a term already present has its last_seen_ts touched rather than
    being duplicated, so re-running discovery never forks a term's history.
    """
    logger = log.get(__name__)
    categories, patterns = load_seeds(path)
    ts = now()
    inserted = skipped = excluded = 0

    for category, terms in categories.items():
        for raw in terms or []:
            term = raw.strip()
            if not term:
                continue
            if is_excluded(term, patterns):
                excluded += 1
                continue
            cursor = conn.execute(
                "INSERT INTO terms "
                "(term, normalized, category, origin, first_seen_ts, last_seen_ts) "
                "VALUES (?, ?, ?, 'seed', ?, ?) "
                "ON CONFLICT(term) DO UPDATE SET last_seen_ts = ?",
                (term, normalize(term), category, ts, ts, ts),
            )
            if cursor.rowcount and conn.total_changes:
                inserted += 1
            else:
                skipped += 1

    logger.info(
        "seeded",
        extra={"categories": len(categories), "written": inserted, "excluded": excluded},
    )
    return active_terms(conn)


def active_terms(conn: sqlite3.Connection, *, starred_only: bool = False) -> list[Term]:
    sql = "SELECT id, term, normalized, starred FROM terms WHERE status = 'active'"
    if starred_only:
        sql += " AND starred = 1"
    return [
        Term(id=r["id"], term=r["term"], normalized=r["normalized"], starred=bool(r["starred"]))
        for r in conn.execute(sql + " ORDER BY id")
    ]
