"""Rate limiting and backoff. No sleeping, no network."""
import pytest

from radar.collectors.base import RateLimiter, SourceUnavailable


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

    def monotonic(self):
        return self.t


def limiter(clock, **kw):
    return RateLimiter(sleep=clock.sleep, monotonic=clock.monotonic, **kw)


def test_calls_are_spaced_by_the_minimum_interval():
    clock = Clock()
    rl = limiter(clock, min_interval_s=4.0)
    rl.call(lambda: "a")
    rl.call(lambda: "b")
    assert clock.slept == [4.0]


def test_a_transient_failure_is_retried_and_succeeds():
    clock = Clock()
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("429")
        return "ok"

    assert limiter(clock, min_interval_s=0).call(flaky) == "ok"
    assert len(attempts) == 3


def test_persistent_failure_degrades_rather_than_leaking_the_error():
    clock = Clock()

    def dead():
        raise RuntimeError("connection reset")

    # The runner keys off SourceUnavailable to degrade; anything else kills the run.
    with pytest.raises(SourceUnavailable, match="connection reset"):
        limiter(clock, min_interval_s=0, max_retries=3).call(dead)


def test_backoff_grows_and_stays_within_its_ceiling():
    rl = RateLimiter(base_backoff_s=5.0)
    for attempt in range(4):
        assert 0 <= rl.backoff_delay(attempt) <= 5.0 * (2**attempt)
