"""Supply counters. The honest half of the system.

Saturation is **counted, never predicted** (decision 11): it is directly
measurable, so modelling it would only add error. Raw counts are stored with a
timestamp; the LOW/MED/HIGH label is derived at score time so the thresholds
stay tunable without re-collecting.

What the supply table in DATA_SOURCES promised versus what is actually
reachable, measured 2026-09-05:

| Source        | Status | Why                                              |
|---------------|--------|--------------------------------------------------|
| GitHub        | works  | search API, `total_count`, no key                |
| Gumroad       | works  | `/discover` embeds `search_results.total`        |
| Shopify apps  | no     | results render client-side; no count in the HTML |
| Etsy          | no     | HTTP 403 to any non-browser request              |
| Fiverr        | no     | HTTP 403 to any non-browser request              |
| Product Hunt  | no     | `Disallow: /search*` under `User-agent: *`; the  |
|               |        | API is the sanctioned path and needs a token     |
| Amazon        | no     | not attempted; physical supply, low value here   |

Etsy and Fiverr would be reachable by sending a browser User-Agent. That is
exactly the line PROMPT.md draws -- identify honestly, respect the block -- so
they stay unavailable rather than pretended.
"""
from __future__ import annotations

import re
import sqlite3
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

from . import log
from .collectors.base import RateLimiter, SourceUnavailable, Term
from .collectors.http import USER_AGENT, JsonHttp

GITHUB_SEARCH = "https://api.github.com/search/repositories"
GUMROAD_DISCOVER = "https://gumroad.com/discover"


@dataclass
class SupplyCount:
    term_id: int
    source: str
    count: int
    ts: int


class SaturationCounter(ABC):
    """Counts how many people are already selling into a term."""

    source: str

    @abstractmethod
    def count(self, term: Term) -> int: ...


class GitHubSaturation(SaturationCounter):
    """Repositories matching the phrase. Is this already commoditised in OSS?"""

    source = "github"

    def __init__(self, http: JsonHttp | None = None) -> None:
        self.http = http or JsonHttp(self.source, rate_limiter=RateLimiter(min_interval_s=7.0))

    def count(self, term: Term) -> int:
        payload = self.http.get(GITHUB_SEARCH, params={"q": f'"{term.term}"', "per_page": 1})
        total = payload.get("total_count")
        if total is None:
            raise ValueError("no total_count in response")
        return int(total)


class GumroadSaturation(SaturationCounter):
    """Digital products already on sale for the term.

    Gumroad renders its results client-side but ships the search state as JSON
    inside the HTML, so the count comes from there rather than from parsing a
    product grid that changes every redesign.
    """

    source = "gumroad"
    # "search_results":{"total":19875  -- HTML-escaped in the page source.
    TOTAL_RE = re.compile(r"search_results&quot;:\{&quot;total&quot;:(\d+)")
    TOTAL_RE_PLAIN = re.compile(r'"search_results"\s*:\s*\{\s*"total"\s*:\s*(\d+)')

    def __init__(self, *, opener=urllib.request.urlopen, timeout_s: float = 25.0) -> None:
        self.rate_limiter = RateLimiter(min_interval_s=2.0)
        self._opener = opener
        self.timeout_s = timeout_s

    def _fetch(self, term: str) -> str:
        url = f"{GUMROAD_DISCOVER}?{urllib.parse.urlencode({'query': term})}"

        def call() -> str:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with self._opener(request, timeout=self.timeout_s) as response:
                return response.read().decode("utf-8", "replace")

        return self.rate_limiter.call(call, describe=f"{self.source} GET")

    def count(self, term: Term) -> int:
        body = self._fetch(term.term)
        match = self.TOTAL_RE.search(body) or self.TOTAL_RE_PLAIN.search(body)
        if not match:
            # The embedded JSON moved. Better to fail this term loudly than to
            # store a zero that reads as "nobody is selling this".
            raise ValueError("search_results.total not found in page")
        return int(match.group(1))


def build_counters(enabled: set[str], *, http: JsonHttp | None = None) -> list[SaturationCounter]:
    counters: list[SaturationCounter] = []
    if "github" in enabled:
        counters.append(GitHubSaturation(http=http))
    if "gumroad" in enabled:
        counters.append(GumroadSaturation())
    return counters


def collect_saturation(
    conn: sqlite3.Connection,
    counters: list[SaturationCounter],
    terms: list[Term],
    run_id: str,
) -> int:
    """Count supply for every term, persist raw counts, return rows written.

    One counter failing a term is partial. One counter failing every term is
    that counter's problem, not the run's: the others still write.
    """
    logger = log.get(__name__, run_id=run_id)
    written = 0

    for counter in counters:
        failures = 0
        counts: list[SupplyCount] = []
        for term in terms:
            try:
                value = counter.count(term)
            except SourceUnavailable as exc:
                logger.error(
                    "saturation source down",
                    extra={"source": counter.source, "error": str(exc)},
                )
                failures = len(terms)
                break
            except Exception as exc:  # noqa: BLE001 -- one term, not the source
                failures += 1
                logger.warning(
                    "saturation term failed",
                    extra={"source": counter.source, "term": term.term, "error": str(exc)},
                )
                continue
            counts.append(
                SupplyCount(
                    term_id=term.id, source=counter.source, count=value, ts=int(time.time())
                )
            )

        for row in counts:
            conn.execute(
                "INSERT INTO saturation_snapshots (term_id, source, count, ts, run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (row.term_id, row.source, row.count, row.ts, run_id),
            )
            written += 1

        logger.info(
            "saturation counted",
            extra={"source": counter.source, "terms": len(terms), "written": len(counts),
                   "failures": failures},
        )

    return written


def label(count: int, *, low_max: int, med_max: int) -> str:
    """Raw count to LOW/MED/HIGH. Derived at score time, never stored."""
    if count <= low_max:
        return "LOW"
    if count <= med_max:
        return "MED"
    return "HIGH"


def latest_counts(conn: sqlite3.Connection, term_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT source, count FROM saturation_snapshots s WHERE term_id = ? "
        "AND ts = (SELECT MAX(ts) FROM saturation_snapshots WHERE term_id = s.term_id "
        "AND source = s.source)",
        (term_id,),
    ).fetchall()
    return {r["source"]: r["count"] for r in rows}
