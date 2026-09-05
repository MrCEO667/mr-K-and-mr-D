"""YouTube Data API v3.

> Retrend implemented this fully and got zero rows because no API key was ever
> provisioned. The code was fine; the key was missing.

So this collector asserts the key at construction, and the runner's preflight
refuses to start an enabled source without its credentials.

**Quota is counted in units, not requests.** 10,000 units/day: `search.list`
costs 100, `videos.list` costs 1. That is why the search runs once per term and
the view counts are hydrated in batches of fifty. A sweep over 25 terms costs
about 2,502 units, so three sweeps a day fit and a fourth does not.

Two metrics, and they are not equally trustworthy:

* `view_sum` -- summed views of the top videos. A real, counted number.
* `video_count` -- YouTube's `totalResults`, which is an **estimate** and
  routinely inflated. Stored because its relative movement is still
  informative, but never present it as a count of anything.
"""
from __future__ import annotations

import os
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

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

SEARCH_COST = 100
VIDEOS_COST = 1
HYDRATE_BATCH = 50

WINDOW_DAYS = 90
RESULTS_PER_TERM = 10
EVIDENCE_PER_TERM = 2


class YouTubeCollector(Collector):
    source = "youtube"
    quota_per_day = 10000  # units, not requests

    def __init__(
        self,
        http: JsonHttp | None = None,
        *,
        api_key: str | None = None,
        window_days: int = WINDOW_DAYS,
    ) -> None:
        self.api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
        if not self.api_key:
            raise SourceUnavailable(
                "YOUTUBE_API_KEY is not set. This is the failure that produced "
                "zero rows in the predecessor project; see DATA_SOURCES."
            )
        self.http = http or JsonHttp(self.source)
        self.window_days = window_days
        self.units_used = 0
        self._failures = 0
        self._terms_seen = 0
        self._latency_ms: int | None = None

    def _spend(self, units: int) -> None:
        """Quota is declared and enforced, never discovered by hitting it."""
        if self.units_used + units > self.quota_per_day:
            raise QuotaExceeded(
                f"{self.source}: {self.units_used}/{self.quota_per_day} units used, "
                f"next call costs {units}"
            )
        self.units_used += units

    def _search(self, term: str) -> dict:
        published_after = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - self.window_days * 86400)
        )
        self._spend(SEARCH_COST)
        return self.http.get(
            SEARCH_URL,
            params={
                "part": "snippet",
                "q": f'"{term}"',
                "type": "video",
                "order": "relevance",
                "maxResults": RESULTS_PER_TERM,
                "publishedAfter": published_after,
                "key": self.api_key,
            },
        )

    def _hydrate(self, video_ids: list[str]) -> dict[str, dict]:
        """Batch the statistics lookup: fifty ids for one unit, not fifty."""
        stats: dict[str, dict] = {}
        for start in range(0, len(video_ids), HYDRATE_BATCH):
            chunk = video_ids[start : start + HYDRATE_BATCH]
            self._spend(VIDEOS_COST)
            payload = self.http.get(
                VIDEOS_URL,
                params={
                    "part": "statistics,snippet",
                    "id": ",".join(chunk),
                    "key": self.api_key,
                },
            )
            for item in payload.get("items") or []:
                stats[item["id"]] = item
        return stats

    def collect(self, terms: list[Term], run_id: str) -> CollectResult:
        logger = log.get(__name__, run_id=run_id, source=self.source)
        result = CollectResult()
        ts = int(time.time())

        found: dict[int, list[str]] = {}
        for term in terms:
            self._terms_seen += 1
            started = time.monotonic()
            try:
                payload = self._search(term.term)
            except (SourceUnavailable, QuotaExceeded):
                raise
            except Exception as exc:  # noqa: BLE001 -- one term, not the source
                self._failures += 1
                result.partial = True
                result.errors.append(f"{term.term}: {type(exc).__name__}: {exc}")
                continue
            finally:
                self._latency_ms = int((time.monotonic() - started) * 1000)

            total = (payload.get("pageInfo") or {}).get("totalResults")
            if total is not None:
                result.readings.append(
                    Reading(
                        term_id=term.id,
                        source=self.source,
                        metric="video_count",
                        value=float(total),
                        ts=ts,
                    )
                )
            ids = [
                item["id"]["videoId"]
                for item in (payload.get("items") or [])
                if (item.get("id") or {}).get("videoId")
            ]
            if ids:
                found[term.id] = ids

        all_ids = [vid for ids in found.values() for vid in ids]
        stats = self._hydrate(all_ids) if all_ids else {}

        for term_id, ids in found.items():
            views = 0
            for vid in ids:
                item = stats.get(vid)
                if item:
                    views += int((item.get("statistics") or {}).get("viewCount") or 0)
            result.readings.append(
                Reading(
                    term_id=term_id,
                    source=self.source,
                    metric="view_sum",
                    value=float(views),
                    ts=ts,
                )
            )
            for vid in ids[:EVIDENCE_PER_TERM]:
                item = stats.get(vid)
                if not item:
                    continue
                result.evidence.append(
                    EvidenceItem(
                        term_id=term_id,
                        source=self.source,
                        url=f"https://www.youtube.com/watch?v={vid}",
                        title=(item.get("snippet") or {}).get("title"),
                        snippet=None,
                        metric_json={
                            "views": (item.get("statistics") or {}).get("viewCount"),
                            "published_at": (item.get("snippet") or {}).get("publishedAt"),
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
                "units_used": self.units_used,
                "partial": result.partial,
            },
        )
        return result

    def health(self) -> SourceHealth:
        if self._terms_seen == 0 or self._failures == 0:
            status = "ok"
        elif self._failures >= self._terms_seen:
            status = "down"
        else:
            status = "degraded"
        detail = f"{self.units_used} units used"
        return SourceHealth(
            source=self.source,
            status=status,
            latency_ms=self._latency_ms,
            error_count=self._failures,
            message=detail if status == "ok" else f"{self._failures} failure(s), {detail}",
        )
