"""Product Hunt via the GraphQL v2 API.

Two signals from one source, which is why it is worth the token:

* demand -- how many launches match the term, and how they did
* supply  -- somebody already shipped this, which is saturation evidence

Scraping the site instead is not an option: `producthunt.com/robots.txt`
carries `Disallow: /search*` under `User-agent: *`. The API is the sanctioned
path.

**Unverified against the live API.** Written from the documented schema while
the developer token was still pending, and exercised only against stubs. The
shape may need a correction on first contact; treat a failure here as a bug in
this file before assuming the source is down.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .. import log
from .base import (
    Collector,
    CollectResult,
    EvidenceItem,
    QuotaExceeded,
    RateLimiter,
    Reading,
    SourceHealth,
    SourceUnavailable,
    Term,
)
from .http import USER_AGENT

API_URL = "https://api.producthunt.com/v2/api/graphql"
METRIC = "launch_count"
VOTES_METRIC = "vote_sum"
RESULTS_PER_TERM = 10
EVIDENCE_PER_TERM = 2

QUERY = """
query Search($term: String!, $first: Int!) {
  posts(query: $term, first: $first, order: VOTES) {
    totalCount
    edges {
      node {
        id
        name
        tagline
        url
        votesCount
        createdAt
      }
    }
  }
}
"""


class ProductHuntCollector(Collector):
    source = "product_hunt"
    quota_per_day = 450  # documented complexity budget is per-hour; stay well under

    def __init__(
        self,
        *,
        token: str | None = None,
        opener=urllib.request.urlopen,
        rate_limiter: RateLimiter | None = None,
        timeout_s: float = 25.0,
    ) -> None:
        self.token = token or os.environ.get("PRODUCTHUNT_TOKEN")
        if not self.token:
            raise SourceUnavailable(
                "PRODUCTHUNT_TOKEN is not set. Create a developer token at "
                "producthunt.com/v2/oauth/applications."
            )
        self._opener = opener
        self.rate_limiter = rate_limiter or RateLimiter(min_interval_s=2.0)
        self.timeout_s = timeout_s
        self._requests = 0
        self._failures = 0
        self._latency_ms: int | None = None

    def _query(self, term: str) -> dict:
        if self._requests >= self.quota_per_day:
            raise QuotaExceeded(f"{self.source}: declared daily quota spent")
        payload = json.dumps(
            {"query": QUERY, "variables": {"term": term, "first": RESULTS_PER_TERM}}
        ).encode()

        def call() -> dict:
            request = urllib.request.Request(
                API_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with self._opener(request, timeout=self.timeout_s) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise SourceUnavailable(
                        f"product hunt rejected the token (HTTP {exc.code})"
                    ) from exc
                raise

        self._requests += 1
        started = time.monotonic()
        try:
            body = self.rate_limiter.call(call, describe=f"{self.source} query")
        finally:
            self._latency_ms = int((time.monotonic() - started) * 1000)

        if body.get("errors"):
            # GraphQL returns 200 with an errors array. Silently reading data
            # as None here would store zeros that mean "nobody launched this".
            raise ValueError(f"graphql errors: {body['errors'][:1]}")
        return body

    def collect(self, terms: list[Term], run_id: str) -> CollectResult:
        logger = log.get(__name__, run_id=run_id, source=self.source)
        result = CollectResult()
        ts = int(time.time())

        for term in terms:
            try:
                body = self._query(term.term)
            except (SourceUnavailable, QuotaExceeded):
                raise
            except Exception as exc:  # noqa: BLE001 -- one term, not the source
                self._failures += 1
                result.partial = True
                result.errors.append(f"{term.term}: {type(exc).__name__}: {exc}")
                continue

            posts = ((body.get("data") or {}).get("posts")) or {}
            total = posts.get("totalCount")
            if total is None:
                self._failures += 1
                result.partial = True
                result.errors.append(f"{term.term}: no totalCount in response")
                continue

            result.readings.append(
                Reading(
                    term_id=term.id,
                    source=self.source,
                    metric=METRIC,
                    value=float(total),
                    ts=ts,
                )
            )

            nodes = [edge.get("node") or {} for edge in (posts.get("edges") or [])]
            votes = sum(int(n.get("votesCount") or 0) for n in nodes)
            result.readings.append(
                Reading(
                    term_id=term.id,
                    source=self.source,
                    metric=VOTES_METRIC,
                    value=float(votes),
                    ts=ts,
                )
            )
            for node in nodes[:EVIDENCE_PER_TERM]:
                if not node.get("url"):
                    continue
                result.evidence.append(
                    EvidenceItem(
                        term_id=term.id,
                        source=self.source,
                        url=node["url"],
                        title=node.get("name"),
                        snippet=node.get("tagline"),
                        metric_json={
                            "votes": node.get("votesCount"),
                            "created_at": node.get("createdAt"),
                        },
                    )
                )

        if terms and self._failures >= len(terms):
            last = result.errors[-1] if result.errors else "no detail"
            raise SourceUnavailable(f"every term failed: {last}")

        logger.info(
            "collected",
            extra={
                "terms": len(terms),
                "readings": len(result.readings),
                "partial": result.partial,
            },
        )
        return result

    def health(self) -> SourceHealth:
        if self._requests == 0 or self._failures == 0:
            status = "ok"
        elif self._failures >= self._requests:
            status = "down"
        else:
            status = "degraded"
        return SourceHealth(
            source=self.source,
            status=status,
            latency_ms=self._latency_ms,
            error_count=self._failures,
            message=None if status == "ok" else f"{self._failures} failure(s)",
        )
