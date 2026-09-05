"""Product Hunt via the GraphQL v2 API.

Two signals from one source: how many launches match a term (demand, and also
supply evidence -- somebody already shipped this), and how well they did.

**The API has no keyword search for posts.** This was written first against a
`posts(query: ...)` field that does not exist; introspection says the arguments
are `featured, postedBefore, postedAfter, topic, order, twitterUrl, url,
after, before, first, last`. Only `topics` takes a `query`, and topics are
broad categories, not phrases like "ai voice clone".

So the shape is inverted: scan recent launches **once**, then match every term
against that page locally. Cost is independent of how many terms are seeded,
which is the opposite of YouTube, where each term costs 100 units.

Measured limits, 2026-09-05:

* page size is capped at **20** regardless of `first:`
* each page costs **200 complexity** of a **6250 per 15 minutes** budget
* so about 31 pages, 620 launches, per window

The scan is therefore cached for the day (`radar.cache`), and a run reuses it
rather than re-walking the feed. Scraping the site instead is not an option:
robots.txt disallows `/search*` under `User-agent: *`.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from .. import log
from ..cache import Cache
from .base import (
    Collector,
    CollectResult,
    EvidenceItem,
    RateLimiter,
    Reading,
    SourceHealth,
    SourceUnavailable,
    Term,
)
from .http import USER_AGENT

TOKEN_URL = "https://api.producthunt.com/v2/oauth/token"
API_URL = "https://api.producthunt.com/v2/api/graphql"

METRIC = "launch_count"
VOTES_METRIC = "vote_sum"
EVIDENCE_PER_TERM = 2

PAGE_SIZE = 20  # the API caps here whatever you ask for
COMPLEXITY_PER_PAGE = 200
WINDOW_DAYS = 30
# 20 pages is 4000 of the 6250 complexity window. 30 pages left no headroom:
# the first full sweep 429'd because smoke tests had already spent part of the
# same 15-minute window.
MAX_PAGES = 20

POSTS_QUERY = """
query Recent($after: String, $postedAfter: DateTime!) {
  posts(order: NEWEST, first: 20, postedAfter: $postedAfter, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node { id name tagline url votesCount createdAt }
    }
  }
}
"""


class ProductHuntCollector(Collector):
    source = "product_hunt"
    # Complexity units per 15 minutes, not requests per day. Declared in the
    # unit the API actually bills in, per collector rule 1.
    quota_per_day = 6250

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        token: str | None = None,
        opener=urllib.request.urlopen,
        rate_limiter: RateLimiter | None = None,
        cache: Cache | None = None,
        window_days: int = WINDOW_DAYS,
        max_pages: int = MAX_PAGES,
        timeout_s: float = 25.0,
    ) -> None:
        self.client_id = client_id or os.environ.get("PRODUCTHUNT_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PRODUCTHUNT_CLIENT_SECRET")
        self._token = token or os.environ.get("PRODUCTHUNT_TOKEN")
        if not self._token and not (self.client_id and self.client_secret):
            raise SourceUnavailable(
                "Product Hunt needs PRODUCTHUNT_CLIENT_ID and "
                "PRODUCTHUNT_CLIENT_SECRET (or a PRODUCTHUNT_TOKEN). Create an "
                "application at producthunt.com/v2/oauth/applications."
            )
        self._opener = opener
        self.rate_limiter = rate_limiter or RateLimiter(min_interval_s=1.0)
        self.cache = cache if cache is not None else Cache()
        self.window_days = window_days
        self.max_pages = max_pages
        self.timeout_s = timeout_s
        self.complexity_used = 0
        self._failures = 0
        self._pages = 0
        self._partial_scan: str | None = None
        self._latency_ms: int | None = None

    # -- auth ---------------------------------------------------------------

    def _access_token(self) -> str:
        """client_credentials grant. Public scope is all this needs."""
        if self._token:
            return self._token
        payload = json.dumps(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }
        ).encode()

        def call() -> dict:
            request = urllib.request.Request(
                TOKEN_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with self._opener(request, timeout=self.timeout_s) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401, 403):
                    raise SourceUnavailable(
                        f"product hunt rejected the credentials (HTTP {exc.code})"
                    ) from exc
                raise

        body = self.rate_limiter.call(call, describe="product hunt token")
        token = body.get("access_token")
        if not token:
            raise SourceUnavailable(f"product hunt returned no access_token: {body}")
        self._token = token
        return token

    # -- the scan -----------------------------------------------------------

    def _page(self, token: str, after: str | None, posted_after: str) -> dict:
        if self.complexity_used + COMPLEXITY_PER_PAGE > self.quota_per_day:
            raise SourceUnavailable(
                f"{self.source}: complexity budget spent "
                f"({self.complexity_used}/{self.quota_per_day})"
            )
        payload = json.dumps(
            {
                "query": POSTS_QUERY,
                "variables": {"after": after, "postedAfter": posted_after},
            }
        ).encode()

        def call() -> dict:
            request = urllib.request.Request(
                API_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            with self._opener(request, timeout=self.timeout_s) as response:
                return json.load(response)

        started = time.monotonic()
        try:
            body = self.rate_limiter.call(call, describe="product hunt posts")
        finally:
            self._latency_ms = int((time.monotonic() - started) * 1000)
        self.complexity_used += COMPLEXITY_PER_PAGE
        self._pages += 1

        if body.get("errors"):
            # GraphQL answers 200 with an errors array. Reading that as empty
            # data would store zeros meaning "nobody launched this".
            raise ValueError(f"graphql errors: {body['errors'][:1]}")
        return body

    def recent_launches(self) -> list[dict]:
        """Every launch in the window, cached for the day.

        One scan serves every term, so adding seed terms costs nothing here.
        """
        posted_after = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - self.window_days * 86400)
        )
        cache_key = f"launches:{posted_after}:{self.max_pages}"
        cached = self.cache.get(self.source, cache_key)
        if cached is not None:
            return cached

        token = self._access_token()
        launches: list[dict] = []
        after: str | None = None
        complete = False
        for _ in range(self.max_pages):
            try:
                body = self._page(token, after, posted_after)
            except SourceUnavailable as exc:
                # Rate limited or budget spent part way through. Pages already
                # walked are real launches; throwing them away to report
                # nothing would be worse than reporting fewer.
                self._partial_scan = str(exc)
                break
            posts = ((body.get("data") or {}).get("posts")) or {}
            for edge in posts.get("edges") or []:
                node = edge.get("node") or {}
                if node:
                    launches.append(node)
            page_info = posts.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                complete = True
                break
            after = page_info.get("endCursor")
            if not after:
                complete = True
                break
        else:
            complete = True

        if complete:
            # Only a complete scan is cached: storing a truncated one would
            # freeze the short answer in for the rest of the day.
            self.cache.put(self.source, cache_key, launches)
        return launches

    # -- collector ----------------------------------------------------------

    def collect(self, terms: list[Term], run_id: str) -> CollectResult:
        logger = log.get(__name__, run_id=run_id, source=self.source)
        result = CollectResult()
        ts = int(time.time())

        try:
            launches = self.recent_launches()
        except SourceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            raise SourceUnavailable(f"launch scan failed: {type(exc).__name__}: {exc}") from exc

        haystack = [
            (
                f"{post.get('name') or ''} {post.get('tagline') or ''}".lower(),
                post,
            )
            for post in launches
        ]

        for term in terms:
            # Word boundaries, not substring: plain `in` made the term "ai"
            # match 39 of 160 launches by finding it inside "email",
            # "training" and "explain".
            pattern = re.compile(rf"\b{re.escape(term.term.lower())}\b")
            matches = [post for text, post in haystack if pattern.search(text)]

            result.readings.append(
                Reading(
                    term_id=term.id,
                    source=self.source,
                    metric=METRIC,
                    value=float(len(matches)),
                    ts=ts,
                )
            )
            result.readings.append(
                Reading(
                    term_id=term.id,
                    source=self.source,
                    metric=VOTES_METRIC,
                    value=float(sum(int(p.get("votesCount") or 0) for p in matches)),
                    ts=ts,
                )
            )
            for post in sorted(
                matches, key=lambda p: int(p.get("votesCount") or 0), reverse=True
            )[:EVIDENCE_PER_TERM]:
                if not post.get("url"):
                    continue
                result.evidence.append(
                    EvidenceItem(
                        term_id=term.id,
                        source=self.source,
                        url=post["url"],
                        title=post.get("name"),
                        snippet=post.get("tagline"),
                        metric_json={
                            "votes": post.get("votesCount"),
                            "created_at": post.get("createdAt"),
                        },
                    )
                )

        if self._partial_scan:
            result.partial = True
            result.errors.append(f"scan truncated: {self._partial_scan}")

        if not launches:
            raise SourceUnavailable(
                f"no launches scanned: {self._partial_scan or 'empty feed'}"
            )

        logger.info(
            "collected",
            extra={
                "terms": len(terms),
                "launches_scanned": len(launches),
                "scan_complete": self._partial_scan is None,
                "pages": self._pages,
                "complexity_used": self.complexity_used,
                "readings": len(result.readings),
            },
        )
        return result

    def health(self) -> SourceHealth:
        if self._failures:
            status = "down"
        elif self._partial_scan:
            status = "degraded"
        else:
            status = "ok"
        return SourceHealth(
            source=self.source,
            status=status,
            latency_ms=self._latency_ms,
            error_count=self._failures,
            message=f"{self._pages} pages, {self.complexity_used} complexity",
        )
