"""Google Trends collector.

The backbone source: no key, no cost, and up to five years of history on the
first call, which is what makes the durability model trainable on day one
rather than after six months of collecting.

**The anchor.** Trends values are relative -- 0-100 normalised *within a single
request*. A term scoring 80 in one request and 40 in another has not halved;
the two numbers simply do not share a scale. Every batch here therefore
includes one fixed anchor term, and values are rescaled against the anchor's
mean before being stored. Without this, M5 would train on noise that looks
exactly like signal.

Trends allows five terms per request, so each batch carries the anchor plus
four real terms.
"""
from __future__ import annotations

import time
from typing import Any

from .. import log
from .base import (
    Collector,
    CollectResult,
    QuotaExceeded,
    RateLimiter,
    Reading,
    SourceHealth,
    SourceUnavailable,
    Term,
)

# The anchor is a ruler, and a ruler has to be the right size. Trends returns
# integers 0-100 normalised within the request, so an anchor far more popular
# than the terms crushes all of them to literal zero: measured against
# "weather", every seed term in this project returned 0.000 for a full quarter.
# The anchor must therefore sit in the same volume band as the terms it scales.
# It must also sit outside config/seeds.yaml: an anchor that is itself a
# collected term ends up in its own request twice, which Trends rejects, and
# could only ever score 1.0 against itself.
# Its own values are never stored. Changing it makes new readings incomparable
# with old ones, so it is a decision (DECISIONS 18), not a tuning knob.
ANCHOR_TERM = "wordpress plugin"
MAX_TERMS_PER_REQUEST = 5
BATCH_SIZE = MAX_TERMS_PER_REQUEST - 1  # one slot is always the anchor

METRIC = "interest"

# Trends returns DAILY points only for an explicit date range of roughly eight
# months or less. "today 5-y" and "today 12-m" both come back weekly, which is
# useless for a 14-day feature window. Multi-year daily history is therefore
# chained: overlapping date-range requests, each carrying the anchor, each
# rescaled by its own anchor mean so the chunks share a scale.
CHUNK_DAYS = 240
CHUNK_OVERLAP_DAYS = 30


class ZeroSeries(RuntimeError):
    """A term that came back as all zeros against the anchor. A term-level
    problem, never a source-level one."""


class TrendsCollector(Collector):
    source = "google_trends"
    quota_per_day = 1440  # self-imposed; Google publishes none

    def __init__(
        self,
        client: Any = None,
        *,
        rate_limiter: RateLimiter | None = None,
        timeframe: str = "today 3-m",
        geo: str = "",
        anchor: str = ANCHOR_TERM,
    ) -> None:
        self._client = client
        self.rate_limiter = rate_limiter or RateLimiter(min_interval_s=4.0)
        self.timeframe = timeframe
        self.geo = geo
        self.anchor = anchor
        # A batch that never came back and a term Trends declined to report
        # are different failures: the first says the source is sick, the second
        # says one query has too little volume. Conflating them marks a working
        # source dead.
        self._request_failures = 0
        self._term_failures = 0
        self._last_latency_ms: int | None = None
        self._requests_made = 0

    @property
    def client(self) -> Any:
        """Built lazily so importing the module never needs the network."""
        if self._client is None:
            try:
                from pytrends.request import TrendReq
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise SourceUnavailable(f"pytrends is not installed: {exc}") from exc
            self._client = TrendReq(hl="en-US", tz=0)
        return self._client

    def _fetch_batch(self, terms: list[str]) -> Any:
        """One Trends request: anchor + up to four terms, returned as a frame.

        The anchor is filtered out of the terms: sending a keyword twice makes
        Trends reject the whole request, which cost a batch of 4 terms the
        first time this ran for real.
        """
        payload = [self.anchor, *[t for t in terms if t != self.anchor]]

        def call() -> Any:
            started = time.monotonic()
            self.client.build_payload(payload, timeframe=self.timeframe, geo=self.geo)
            frame = self.client.interest_over_time()
            self._last_latency_ms = int((time.monotonic() - started) * 1000)
            return frame

        self._requests_made += 1
        if self._requests_made > self.quota_per_day:
            raise QuotaExceeded(f"{self.source}: declared daily quota spent")
        return self.rate_limiter.call(call, describe=f"trends batch {payload}")

    def _rescale(self, frame: Any, column: str) -> list[tuple[int, float]]:
        """Return (unix_ts, anchored_value) for one column.

        Dividing by the anchor's mean puts every request on one scale. A batch
        whose anchor came back flat zero is unusable rather than merely small,
        so it is dropped instead of being stored as a misleading number.

        The incomplete final period is dropped: Trends marks it isPartial and
        revises it later, so storing it appends an observation that will change
        underneath the model.
        """
        rows = frame
        if "isPartial" in frame:
            rows = frame[~frame["isPartial"].astype(bool)]
        if len(rows) == 0:
            raise ZeroSeries(f"{column}: nothing but incomplete periods")

        anchor_mean = float(rows[self.anchor].mean())
        if anchor_mean <= 0:
            raise SourceUnavailable(
                f"anchor {self.anchor!r} returned no signal; batch is unscalable"
            )

        values = rows[column]
        if float(values.max()) <= 0:
            # Every point rounded to zero against this anchor. That is an
            # anchor too large for the term, not a term with no demand, and
            # storing it would be 90 days of fabricated silence.
            raise ZeroSeries(
                f"{column}: flat zero against anchor {self.anchor!r}; "
                "the anchor is too popular for this term"
            )

        return [
            (int(stamp.timestamp()), float(value) / anchor_mean)
            for stamp, value in values.items()
        ]

    def collect(self, terms: list[Term], run_id: str) -> CollectResult:
        logger = log.get(__name__, run_id=run_id, source=self.source)
        result = CollectResult()
        if not terms:
            return result

        by_query = {t.term: t for t in terms if t.term != self.anchor}
        if len(by_query) != len(terms):
            logger.warning(
                "term skipped: it is the anchor and cannot be scaled against itself",
                extra={"term": self.anchor},
            )
        batches = [
            list(by_query)[i : i + BATCH_SIZE] for i in range(0, len(by_query), BATCH_SIZE)
        ]

        batches_failed = 0
        for batch in batches:
            try:
                frame = self._fetch_batch(batch)
            except QuotaExceeded:
                # The budget is spent; further requests are pointless, not sick.
                raise
            except SourceUnavailable as exc:
                # This batch exhausted its retries. Trends 429s as a matter of
                # routine, and the next batch after a backoff often succeeds,
                # so one exhausted batch is partial -- not a dead source.
                batches_failed += 1
                self._request_failures += 1
                result.partial = True
                result.errors.append(f"batch {batch}: {exc}")
                logger.warning("batch exhausted retries", extra={"batch": batch})
                continue
            except Exception as exc:  # noqa: BLE001 -- one batch, not the source
                self._request_failures += 1
                result.partial = True
                result.errors.append(f"batch {batch}: {type(exc).__name__}: {exc}")
                logger.warning("batch failed", extra={"batch": batch, "error": str(exc)})
                continue

            if frame is None or len(frame) == 0:
                self._request_failures += 1
                result.partial = True
                result.errors.append(f"batch {batch}: empty frame")
                continue

            for query in batch:
                term = by_query[query]
                if query not in frame:
                    # Trends drops terms with too little volume to report.
                    self._term_failures += 1
                    result.partial = True
                    result.errors.append(f"{query}: not returned by Trends (low volume)")
                    continue
                try:
                    points = self._rescale(frame, query)
                except ZeroSeries as exc:
                    self._term_failures += 1
                    result.partial = True
                    result.errors.append(str(exc))
                    logger.warning("term unscalable", extra={"term": query, "why": str(exc)})
                    continue
                except SourceUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._term_failures += 1
                    result.partial = True
                    result.errors.append(f"{query}: {type(exc).__name__}: {exc}")
                    continue
                for ts, value in points:
                    result.readings.append(
                        Reading(
                            term_id=term.id,
                            source=self.source,
                            metric=METRIC,
                            value=value,
                            ts=ts,
                        )
                    )

        if batches and batches_failed == len(batches):
            # Nothing got through at all. Now it is the source, and the run
            # degrades on it rather than storing a convincing silence.
            last = result.errors[-1] if result.errors else "no detail"
            raise SourceUnavailable(f"all {len(batches)} batches failed; last: {last}")

        logger.info(
            "collected",
            extra={
                "terms": len(terms),
                "batches": len(batches),
                "readings": len(result.readings),
                "partial": result.partial,
            },
        )
        return result

    def history(
        self,
        terms: list[Term],
        run_id: str,
        *,
        days: int = 720,
    ) -> CollectResult:
        """Backfill daily history by walking overlapping date-range chunks.

        This is the unlock M5 depends on: the past of every window is already
        known, so labels are free. It is a separate operation from a sweep --
        it is run rarely and costs many requests.
        """
        import datetime as _dt

        logger = log.get(__name__, run_id=run_id, source=self.source)
        result = CollectResult()
        by_query = {t.term: t for t in terms if t.term != self.anchor}
        today = _dt.date.today()

        starts: list[_dt.date] = []
        cursor = today - _dt.timedelta(days=days)
        while cursor < today:
            starts.append(cursor)
            cursor += _dt.timedelta(days=CHUNK_DAYS - CHUNK_OVERLAP_DAYS)

        batches = [
            list(by_query)[i : i + BATCH_SIZE] for i in range(0, len(by_query), BATCH_SIZE)
        ]

        seen: set[tuple[int, int]] = set()
        for start in starts:
            end = min(start + _dt.timedelta(days=CHUNK_DAYS), today)
            timeframe = f"{start.isoformat()} {end.isoformat()}"
            for batch in batches:
                previous, self.timeframe = self.timeframe, timeframe
                try:
                    frame = self._fetch_batch(batch)
                except QuotaExceeded:
                    # Spent, not sick. Stop asking, but keep what was collected.
                    result.partial = True
                    result.errors.append(f"{timeframe} {batch}: quota exceeded")
                    logger.warning("history stopped on quota", extra={"timeframe": timeframe})
                    return result
                except SourceUnavailable as exc:
                    self._request_failures += 1
                    result.partial = True
                    result.errors.append(f"{timeframe} {batch}: {exc}")
                    logger.warning("history chunk failed", extra={"timeframe": timeframe})
                    continue
                except Exception as exc:  # noqa: BLE001 -- one chunk, not the run
                    # collect() degrades on an unexpected error; history() used
                    # to let it propagate, which threw away a four-minute
                    # backfill of everything already collected, because the
                    # caller only writes readings after this returns.
                    self._request_failures += 1
                    result.partial = True
                    result.errors.append(f"{timeframe} {batch}: {type(exc).__name__}: {exc}")
                    logger.warning(
                        "history chunk failed",
                        extra={"timeframe": timeframe, "error": str(exc)},
                    )
                    continue
                finally:
                    self.timeframe = previous

                if frame is None or len(frame) == 0:
                    result.partial = True
                    continue

                for query in batch:
                    if query not in frame:
                        # Trends dropped the term from the response. Silently
                        # skipping left no trace, so health() reported the
                        # source healthier than it was.
                        self._term_failures += 1
                        result.partial = True
                        result.errors.append(f"{query} {timeframe}: missing from response")
                        continue
                    term = by_query[query]
                    try:
                        points = self._rescale(frame, query)
                    except (ZeroSeries, SourceUnavailable) as exc:
                        self._term_failures += 1
                        result.partial = True
                        result.errors.append(f"{query} {timeframe}: {exc}")
                        continue
                    for ts, value in points:
                        key = (term.id, ts)
                        if key in seen:
                            # Chunks overlap by design; the first reading for a
                            # day wins so a stitched series has one value a day.
                            continue
                        seen.add(key)
                        result.readings.append(
                            Reading(
                                term_id=term.id,
                                source=self.source,
                                metric=METRIC,
                                value=value,
                                ts=ts,
                            )
                        )

            logger.info(
                "history chunk done",
                extra={"timeframe": timeframe, "readings_so_far": len(result.readings)},
            )

        return result

    def health(self) -> SourceHealth:
        errors = self._request_failures + self._term_failures
        if self._requests_made == 0 or errors == 0:
            status = "ok"
        elif self._request_failures >= self._requests_made:
            # Every request failed: the source, not the query, is the problem.
            status = "down"
        else:
            status = "degraded"
        return SourceHealth(
            source=self.source,
            status=status,
            latency_ms=self._last_latency_ms,
            error_count=errors,
            message=None if status == "ok" else f"{errors} failure(s)",
        )
