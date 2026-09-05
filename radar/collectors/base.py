"""The Collector contract, shared types, and rate limiting.

Frozen interface -- see schema/contracts.md. The rules that matter:

* A single term failing is never an exception. Set partial=True and carry on.
* SourceUnavailable means the whole source is down, and degrades the run.
* Collectors never touch the database. They return data; the runner persists
  it. That is what keeps "no writing to signal_snapshots outside the collector
  layer" enforceable instead of aspirational.
"""
from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import log


class SourceUnavailable(RuntimeError):
    """The whole source is down. Degrade the run; never kill it."""


class QuotaExceeded(SourceUnavailable):
    """The declared daily quota is spent. Distinct so the runner can say so."""


@dataclass(frozen=True)
class Term:
    id: int
    term: str
    normalized: str
    starred: bool = False


@dataclass
class SourceHealth:
    source: str
    status: str  # ok | degraded | down
    latency_ms: int | None = None
    error_count: int = 0
    message: str | None = None


@dataclass
class Reading:
    term_id: int
    source: str
    metric: str
    value: float
    ts: int


@dataclass
class EvidenceItem:
    term_id: int
    source: str
    url: str
    title: str | None = None
    snippet: str | None = None
    metric_json: dict | None = None


@dataclass
class CollectResult:
    readings: list[Reading] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    partial: bool = False
    errors: list[str] = field(default_factory=list)


class RateLimiter:
    """Minimum spacing between calls, plus exponential backoff with jitter.

    Google Trends 429s as a matter of routine, not as a fault, so backoff is
    part of normal operation rather than error handling. Jitter matters because
    a fixed schedule from a single IP is the pattern that gets throttled harder.
    """

    def __init__(
        self,
        min_interval_s: float = 4.0,
        *,
        max_retries: int = 4,
        base_backoff_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call: float | None = None

    def wait(self) -> None:
        if self._last_call is not None:
            elapsed = self._monotonic() - self._last_call
            if elapsed < self.min_interval_s:
                self._sleep(self.min_interval_s - elapsed)
        self._last_call = self._monotonic()

    def backoff_delay(self, attempt: int) -> float:
        """Exponential with full jitter: base * 2**attempt, randomised down."""
        ceiling = self.base_backoff_s * (2**attempt)
        return random.uniform(0, ceiling)  # noqa: S311 -- spacing, not crypto

    def call(self, fn: Callable[[], object], *, describe: str = "request") -> object:
        """Run fn with spacing and retries. Raises SourceUnavailable if every
        attempt fails, so the caller degrades instead of guessing."""
        logger = log.get(__name__)
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.wait()
            try:
                return fn()
            except SourceUnavailable:
                # A 403 or a dead host is a verdict, not a hiccup. Retrying it
                # only burns the backoff budget the next source will need.
                raise
            except Exception as exc:  # noqa: BLE001 -- the client raises anything
                last_error = exc
                delay = self.backoff_delay(attempt)
                logger.warning(
                    "request failed, backing off",
                    extra={
                        "what": describe,
                        "attempt": attempt + 1,
                        "of": self.max_retries,
                        "delay_s": round(delay, 2),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                self._sleep(delay)
        raise SourceUnavailable(
            f"{describe} failed after {self.max_retries} attempts: {last_error}"
        ) from last_error


class Collector(ABC):
    source: str
    quota_per_day: int

    @abstractmethod
    def collect(self, terms: list[Term], run_id: str) -> CollectResult: ...

    @abstractmethod
    def health(self) -> SourceHealth: ...
