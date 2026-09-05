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


# --------------------------------------------------------------- harvest
# Harvest reads Hacker News, not Reddit.
#
# Reddit is doubly closed. Its API moved to approval-only under the Responsible
# Builder Policy, so /prefs/apps no longer creates a script app -- it shows the
# policy link instead. And its robots.txt is "User-agent: * / Disallow: /",
# which rules out the public RSS feeds too. An earlier version of this file
# harvested those feeds; that was a rule-2 violation and it is gone.
#
# The HN Algolia API is documented for programmatic use, needs no key, and the
# collector already depends on it.

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HARVEST_WINDOW_DAYS = 30
HARVEST_PAGES = 5
HITS_PER_PAGE = 100
STOP_WORDS = frozenset(
    [
        "a", "about", "after", "all", "also", "an", "and", "any", "are", "as", "at", "be",
        "because", "been", "before", "being", "below", "best", "between", "both", "but", "by",
        "can", "could", "did", "do", "does", "done", "each", "for", "from", "get", "got",
        "guide", "help", "how", "i", "if", "in", "into", "is", "it", "its", "just", "made",
        "make", "me", "more", "most", "my", "need", "new", "no", "not", "now", "of", "on",
        "only", "or", "other", "our", "out", "over", "same", "should", "some", "such", "than",
        "that", "the", "them", "then", "these", "they", "this", "those", "to", "top",
        "tutorial", "use", "used", "using", "very", "via", "want", "was", "we", "were", "what",
        "when", "where", "which", "who", "why", "will", "with", "without", "would", "you",
        "your",
    ]
)


def load_subreddits(path: Path | None = None) -> list[str]:
    data = yaml.safe_load((path or DEFAULT_SEEDS).read_text(encoding="utf-8")) or {}
    return list(data.get("subreddits") or [])


# A harvested phrase must name something sellable. Without this gate the first
# live harvest returned "feel like", "wrong path" and "path can keep" -- Reddit
# titles are conversation, and n-grams of conversation are conversation. This
# is the seeded-not-swept principle (decision 9) applied to harvest: the gate
# is what keeps discovery anchored to things a two-person team could sell.
PRODUCT_NOUNS = frozenset(
    [
        "agency", "api", "app", "audit", "automation", "blog", "bot", "brand",
        "bundle", "calculator", "checklist", "client", "clients", "course",
        "dashboard", "design", "directory", "ebook", "editing", "editor",
        "extension", "funnel", "generator", "gig", "guide", "kit", "landing",
        "lead", "leads", "magnet", "marketplace", "newsletter", "niche",
        "plugin", "podcast", "preset", "pricing", "product", "saas", "script",
        "seo", "service", "shop", "software", "store", "subscription",
        "template", "templates", "theme", "tool", "toolkit", "video",
        "website", "widget", "workflow",
    ]
)

MONTHS = frozenset(
    [
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
    ]
)


def _is_junk(window: list[str]) -> bool:
    """Reject phrases that are dates, counts or pure filler.

    The first live harvest returned exactly one candidate and it was
    "september 2026" -- subreddit titles are full of dates, weekday threads and
    dollar amounts that look like phrases and mean nothing. Relevance was
    already the bottleneck that killed 250 of Retrend's 395 candidates.
    """
    if window[0] in STOP_WORDS or window[-1] in STOP_WORDS:
        return True
    if all(w in STOP_WORDS for w in window):
        return True
    if any(w in MONTHS for w in window):
        return True
    if any(re.fullmatch(r"[0-9][0-9a-z']*", w) for w in window):
        # Any bare number: years, "10k", "2026", "$5000".
        return True
    # Must name something sellable, or it is just conversation.
    return not any(w in PRODUCT_NOUNS for w in window)


def _ngrams(title: str, sizes: tuple[int, ...] = (2, 3)) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9']+", title.lower()) if len(w) > 2]
    grams: list[str] = []
    for size in sizes:
        for i in range(len(words) - size + 1):
            window = words[i : i + size]
            if _is_junk(window):
                continue
            grams.append(" ".join(window))
    return grams


def harvest_hackernews(
    conn: sqlite3.Connection,
    *,
    http=None,
    max_new_terms: int = 50,
    min_mentions: int = 3,
    window_days: int = HARVEST_WINDOW_DAYS,
    pages: int = HARVEST_PAGES,
) -> list[str]:
    """Mine candidate terms from recent Hacker News story titles.

    A phrase must appear at least `min_mentions` times and name something
    sellable before it becomes a term. Both gates exist because the term list
    is expensive: every accepted term costs 100 YouTube quota units on every
    sweep, forever.
    """
    from collections import Counter

    from .collectors.http import JsonHttp

    logger = log.get(__name__)
    client = http or JsonHttp("hackernews")
    _, patterns = load_seeds()
    cutoff = now() - window_days * 86400

    counts: Counter[str] = Counter()
    failed = 0
    for page in range(pages):
        try:
            payload = client.get(
                HN_SEARCH,
                params={
                    "tags": "story",
                    "numericFilters": f"created_at_i>{cutoff}",
                    "hitsPerPage": HITS_PER_PAGE,
                    "page": page,
                },
            )
        except Exception as exc:  # noqa: BLE001 -- one page, not the harvest
            failed += 1
            logger.warning("harvest page failed", extra={"page": page, "error": str(exc)})
            continue
        hits = payload.get("hits") or []
        if not hits:
            break
        for hit in hits:
            title = hit.get("title") or hit.get("story_title") or ""
            counts.update(set(_ngrams(title)))

    existing = {r["normalized"] for r in conn.execute("SELECT normalized FROM terms")}
    ts = now()
    added: list[str] = []
    for phrase, seen in counts.most_common():
        if len(added) >= max_new_terms:
            break
        if seen < min_mentions or phrase in existing or is_excluded(phrase, patterns):
            continue
        conn.execute(
            "INSERT INTO terms "
            "(term, normalized, category, origin, first_seen_ts, last_seen_ts) "
            "VALUES (?, ?, NULL, 'harvest:hackernews', ?, ?) "
            "ON CONFLICT(term) DO NOTHING",
            (phrase, normalize(phrase), ts, ts),
        )
        added.append(phrase)

    logger.info(
        "harvested",
        extra={
            "pages": pages,
            "failed": failed,
            "candidates": len(counts),
            "added": len(added),
        },
    )
    return added
