"""GitHub repository search. No key needed, but 10 requests/minute if
unauthenticated, so the spacing is not optional.

Signal: how much developer attention a term already attracts, measured as the
stars on the best-matching repositories. Best leading indicator for developer
tool niches, and honest about being a lagging one everywhere else.

The repo *count* is a supply signal and belongs to saturation (M4), not here.
"""
from __future__ import annotations

import time

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
from .http import JsonHttp

SEARCH_URL = "https://api.github.com/search/repositories"
METRIC = "stars"
TOP_N = 10
EVIDENCE_PER_TERM = 2
# Unauthenticated search is 10 req/min. 7s spacing keeps us under it with room
# for the retry that a 403 secondary-rate-limit response would otherwise cost.
MIN_INTERVAL_S = 7.0


class GitHubCollector(Collector):
    source = "github"
    quota_per_day = 500  # self-imposed; well under 10/min sustained

    def __init__(self, http: JsonHttp | None = None) -> None:
        self.http = http or JsonHttp(
            self.source, rate_limiter=RateLimiter(min_interval_s=MIN_INTERVAL_S)
        )
        self._requests = 0
        self._failures = 0
        self._latency_ms: int | None = None

    def collect(self, terms: list[Term], run_id: str) -> CollectResult:
        logger = log.get(__name__, run_id=run_id, source=self.source)
        result = CollectResult()
        ts = int(time.time())

        for term in terms:
            if self._requests >= self.quota_per_day:
                raise QuotaExceeded(f"{self.source}: declared daily quota spent")
            started = time.monotonic()
            try:
                payload = self.http.get(
                    SEARCH_URL,
                    params={"q": f'"{term.term}"', "sort": "stars", "per_page": TOP_N},
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

            items = payload.get("items")
            if items is None:
                self._failures += 1
                result.partial = True
                result.errors.append(f"{term.term}: response had no items")
                continue

            stars = sum(int(item.get("stargazers_count") or 0) for item in items)
            result.readings.append(
                Reading(
                    term_id=term.id,
                    source=self.source,
                    metric=METRIC,
                    value=float(stars),
                    ts=ts,
                )
            )
            for item in items[:EVIDENCE_PER_TERM]:
                if not item.get("html_url"):
                    continue
                result.evidence.append(
                    EvidenceItem(
                        term_id=term.id,
                        source=self.source,
                        url=item["html_url"],
                        title=item.get("full_name"),
                        snippet=(item.get("description") or None),
                        metric_json={
                            "stars": item.get("stargazers_count"),
                            "pushed_at": item.get("pushed_at"),
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
