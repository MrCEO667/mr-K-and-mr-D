"""Reddit via OAuth.

`/r/<sub>/new.json` returns **403 Blocked** without credentials -- verified
from this machine with an honest User-Agent, which is the failure DATA_SOURCES
says bit Retrend. There is no unauthenticated path to the API, so this
collector requires a script app.

The public RSS feeds do still answer and are used for term harvesting in
`radar/discover.py`. They carry titles and links only -- no scores, no comment
counts -- which is why this collector still matters for demand.

**Unverified against the live API.** Written while app creation was failing in
Reddit's UI, exercised only against stubs. Treat a failure here as a bug in
this file before assuming Reddit is down.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
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

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SEARCH_URL = "https://oauth.reddit.com/search"
METRIC = "post_count"
SCORE_METRIC = "score_sum"
WINDOW = "month"
RESULTS_PER_TERM = 25
EVIDENCE_PER_TERM = 2


class RedditCollector(Collector):
    source = "reddit"
    quota_per_day = 3000  # 100 req/min with credentials; stay far under

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        user_agent: str | None = None,
        opener=urllib.request.urlopen,
        rate_limiter: RateLimiter | None = None,
        timeout_s: float = 25.0,
    ) -> None:
        self.client_id = client_id or os.environ.get("REDDIT_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("REDDIT_CLIENT_SECRET")
        self.user_agent = user_agent or os.environ.get("REDDIT_USER_AGENT") or "nicheradar/0.1"
        if not (self.client_id and self.client_secret):
            raise SourceUnavailable(
                "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set. The public "
                "JSON endpoints return 403 without them; there is no fallback."
            )
        self._opener = opener
        self.rate_limiter = rate_limiter or RateLimiter(min_interval_s=1.0)
        self.timeout_s = timeout_s
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._requests = 0
        self._failures = 0
        self._latency_ms: int | None = None

    def _authenticate(self) -> str:
        """Client-credentials grant. Script apps get an app-only token."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()

        def call() -> dict:
            request = urllib.request.Request(
                TOKEN_URL,
                data=data,
                headers={
                    "Authorization": f"Basic {basic}",
                    "User-Agent": self.user_agent,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            try:
                with self._opener(request, timeout=self.timeout_s) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise SourceUnavailable(
                        f"reddit rejected the credentials (HTTP {exc.code})"
                    ) from exc
                raise

        payload = self.rate_limiter.call(call, describe="reddit token")
        token = payload.get("access_token")
        if not token:
            raise SourceUnavailable(f"reddit returned no access_token: {payload}")
        self._token = token
        self._token_expires_at = time.time() + float(payload.get("expires_in") or 3600)
        return token

    def _search(self, term: str) -> dict:
        if self._requests >= self.quota_per_day:
            raise QuotaExceeded(f"{self.source}: declared daily quota spent")
        token = self._authenticate()
        query = urllib.parse.urlencode(
            {
                "q": f'"{term}"',
                "sort": "new",
                "t": WINDOW,
                "limit": RESULTS_PER_TERM,
                "type": "link",
            }
        )

        def call() -> dict:
            request = urllib.request.Request(
                f"{SEARCH_URL}?{query}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self.user_agent,
                },
            )
            with self._opener(request, timeout=self.timeout_s) as response:
                return json.load(response)

        self._requests += 1
        started = time.monotonic()
        try:
            return self.rate_limiter.call(call, describe=f"reddit search {term!r}")
        finally:
            self._latency_ms = int((time.monotonic() - started) * 1000)

    def collect(self, terms: list[Term], run_id: str) -> CollectResult:
        logger = log.get(__name__, run_id=run_id, source=self.source)
        result = CollectResult()
        ts = int(time.time())

        for term in terms:
            try:
                payload = self._search(term.term)
            except (SourceUnavailable, QuotaExceeded):
                raise
            except Exception as exc:  # noqa: BLE001 -- one term, not the source
                self._failures += 1
                result.partial = True
                result.errors.append(f"{term.term}: {type(exc).__name__}: {exc}")
                continue

            children = ((payload.get("data") or {}).get("children")) or []
            posts = [child.get("data") or {} for child in children]

            result.readings.append(
                Reading(
                    term_id=term.id,
                    source=self.source,
                    metric=METRIC,
                    value=float(len(posts)),
                    ts=ts,
                )
            )
            result.readings.append(
                Reading(
                    term_id=term.id,
                    source=self.source,
                    metric=SCORE_METRIC,
                    value=float(sum(int(p.get("score") or 0) for p in posts)),
                    ts=ts,
                )
            )
            for post in posts[:EVIDENCE_PER_TERM]:
                permalink = post.get("permalink")
                if not permalink:
                    continue
                result.evidence.append(
                    EvidenceItem(
                        term_id=term.id,
                        source=self.source,
                        url=f"https://www.reddit.com{permalink}",
                        title=post.get("title"),
                        snippet=post.get("subreddit"),
                        metric_json={
                            "score": post.get("score"),
                            "num_comments": post.get("num_comments"),
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
