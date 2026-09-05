"""Hacker News via the Algolia API. No key, no practical limit.

Good for "Show HN" launch signals and for developer-adjacent demand. The query
is **phrase-quoted**: Algolia ORs the words otherwise, and an unquoted
"ai voice clone" returns anything mentioning "ai" -- 219 hits where the phrase
itself has 22. Counting the loose number would be counting noise.
"""
from __future__ import annotations

import time

from .. import log
from .base import (
    Collector,
    CollectResult,
    EvidenceItem,
    QuotaExceeded,
    Reading,
    SourceHealth,
    SourceUnavailable,
    Term,
)
from .http import JsonHttp

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
METRIC = "post_count"
WINDOW_DAYS = 30
EVIDENCE_PER_TERM = 2


class HackerNewsCollector(Collector):
    source = "hackernews"
    quota_per_day = 5000  # self-imposed; Algolia publishes no hard limit

    def __init__(self, http: JsonHttp | None = None, *, window_days: int = WINDOW_DAYS) -> None:
        self.http = http or JsonHttp(self.source)
        self.window_days = window_days
        self._requests = 0
        self._failures = 0
        self._latency_ms: int | None = None

    def collect(self, terms: list[Term], run_id: str) -> CollectResult:
        logger = log.get(__name__, run_id=run_id, source=self.source)
        result = CollectResult()
        cutoff = int(time.time()) - self.window_days * 86400
        ts = int(time.time())

        for term in terms:
            if self._requests >= self.quota_per_day:
                raise QuotaExceeded(f"{self.source}: declared daily quota spent")
            started = time.monotonic()
            try:
                payload = self.http.get(
                    SEARCH_URL,
                    params={
                        "query": f'"{term.term}"',
                        "tags": "story",
                        "numericFilters": f"created_at_i>{cutoff}",
                        "hitsPerPage": EVIDENCE_PER_TERM,
                    },
                )
            except SourceUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 -- one term, not the source
                self._failures += 1
                result.partial = True
                result.errors.append(f"{term.term}: {type(exc).__name__}: {exc}")
                continue
            finally:
                self._requests += 1
                self._latency_ms = int((time.monotonic() - started) * 1000)

            count = payload.get("nbHits")
            if count is None:
                self._failures += 1
                result.partial = True
                result.errors.append(f"{term.term}: response had no nbHits")
                continue

            result.readings.append(
                Reading(
                    term_id=term.id,
                    source=self.source,
                    metric=METRIC,
                    value=float(count),
                    ts=ts,
                )
            )
            for hit in (payload.get("hits") or [])[:EVIDENCE_PER_TERM]:
                object_id = hit.get("objectID")
                if not object_id:
                    continue
                result.evidence.append(
                    EvidenceItem(
                        term_id=term.id,
                        source=self.source,
                        url=hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}",
                        title=hit.get("title"),
                        snippet=None,
                        metric_json={
                            "points": hit.get("points"),
                            "num_comments": hit.get("num_comments"),
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
